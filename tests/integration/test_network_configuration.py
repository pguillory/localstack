"""
This test file captures the _current_ state of returning URLs before making
sweeping changes. This is to ensure that the refactoring does not cause
external breaking behaviour. In the future we can update this test suite to
correspond to the behaviour we want, and we get a todo list of things to
change 😂
"""
import json

import pytest
import requests
import xmltodict
from botocore.auth import SigV4Auth

from localstack import config
from localstack.aws.api.lambda_ import Runtime
from localstack.testing.aws.lambda_utils import is_new_provider, is_old_provider
from localstack.utils.files import new_tmp_file, save_file
from localstack.utils.strings import short_uid

pytestmark = [pytest.mark.only_localstack]


class TestOpenSearch:
    """
    OpenSearch does not respect any customisations and just returns a domain with localhost.localstack.cloud in.
    """

    def test_default_strategy(
        self, opensearch_client, opensearch_wait_for_cluster, assert_host_customisation
    ):
        domain_name = f"domain-{short_uid()}"
        res = opensearch_client.create_domain(DomainName=domain_name)
        opensearch_wait_for_cluster(domain_name)
        endpoint = res["DomainStatus"]["Endpoint"]

        assert_host_customisation(endpoint, use_localstack_cloud=True)

    def test_port_strategy(
        self, monkeypatch, opensearch_client, opensearch_wait_for_cluster, assert_host_customisation
    ):
        monkeypatch.setattr(config, "OPENSEARCH_ENDPOINT_STRATEGY", "port")

        domain_name = f"domain-{short_uid()}"
        res = opensearch_client.create_domain(DomainName=domain_name)
        opensearch_wait_for_cluster(domain_name)
        endpoint = res["DomainStatus"]["Endpoint"]

        if config.is_in_docker:
            assert_host_customisation(endpoint, use_localhost=True)
        else:
            assert_host_customisation(endpoint, custom_host="127.0.0.1")

    def test_path_strategy(
        self, monkeypatch, opensearch_client, opensearch_wait_for_cluster, assert_host_customisation
    ):
        monkeypatch.setattr(config, "OPENSEARCH_ENDPOINT_STRATEGY", "path")

        domain_name = f"domain-{short_uid()}"
        res = opensearch_client.create_domain(DomainName=domain_name)
        opensearch_wait_for_cluster(domain_name)
        endpoint = res["DomainStatus"]["Endpoint"]

        assert_host_customisation(endpoint, use_localstack_hostname=True)


class TestS3:
    @pytest.mark.skipif(
        condition=config.LEGACY_S3_PROVIDER, reason="Not implemented for legacy provider"
    )
    def test_non_us_east_1_location(
        self, s3_resource, s3_client, cleanups, assert_host_customisation
    ):
        bucket_name = f"bucket-{short_uid()}"
        res = s3_client.create_bucket(
            Bucket=bucket_name,
            CreateBucketConfiguration={
                "LocationConstraint": "eu-west-1",
            },
        )

        def cleanup():
            bucket = s3_resource.Bucket(bucket_name)
            bucket.objects.all().delete()
            bucket.object_versions.all().delete()
            bucket.delete()

        cleanups.append(cleanup)

        assert_host_customisation(res["Location"], use_hostname_external=True)

    def test_multipart_upload(self, s3_bucket, s3_client, assert_host_customisation):
        key_name = f"key-{short_uid()}"
        upload_id = s3_client.create_multipart_upload(Bucket=s3_bucket, Key=key_name)["UploadId"]
        part_etag = s3_client.upload_part(
            Bucket=s3_bucket, Key=key_name, Body=b"bytes", PartNumber=1, UploadId=upload_id
        )["ETag"]
        res = s3_client.complete_multipart_upload(
            Bucket=s3_bucket,
            Key=key_name,
            MultipartUpload={"Parts": [{"ETag": part_etag, "PartNumber": 1}]},
            UploadId=upload_id,
        )

        assert_host_customisation(res["Location"], use_hostname_external=True)

    def test_201_response(self, s3_bucket, s3_client, assert_host_customisation):
        key_name = f"key-{short_uid()}"
        body = "body"
        presigned_request = s3_client.generate_presigned_post(
            Bucket=s3_bucket,
            Key=key_name,
            Fields={"success_action_status": "201"},
            Conditions=[{"bucket": s3_bucket}, ["eq", "$success_action_status", "201"]],
        )
        files = {"file": ("my-file", body)}
        res = requests.post(
            presigned_request["url"],
            data=presigned_request["fields"],
            files=files,
            verify=False,
        )
        res.raise_for_status()
        json_response = xmltodict.parse(res.content)["PostResponse"]

        assert_host_customisation(json_response["Location"], use_hostname_external=True)


class TestSQS:
    """
    Test all combinations of:

    * endpoint_strategy
    * sqs_port_external
    * hostname_external
    """

    def test_off_strategy_without_external_port(
        self, monkeypatch, sqs_create_queue, assert_host_customisation
    ):
        monkeypatch.setattr(config, "SQS_ENDPOINT_STRATEGY", "off")

        queue_name = f"queue-{short_uid()}"
        queue_url = sqs_create_queue(QueueName=queue_name)

        assert_host_customisation(queue_url, use_localhost=True)
        assert queue_name in queue_url

    def test_off_strategy_with_external_port(
        self, monkeypatch, sqs_create_queue, assert_host_customisation
    ):
        external_port = 12345
        monkeypatch.setattr(config, "SQS_ENDPOINT_STRATEGY", "off")
        monkeypatch.setattr(config, "SQS_PORT_EXTERNAL", external_port)

        queue_name = f"queue-{short_uid()}"
        queue_url = sqs_create_queue(QueueName=queue_name)

        assert_host_customisation(queue_url, use_hostname_external=True)
        assert queue_name in queue_url
        assert f":{external_port}" in queue_url

    def test_domain_strategy(self, monkeypatch, sqs_create_queue, assert_host_customisation):
        monkeypatch.setattr(config, "SQS_ENDPOINT_STRATEGY", "domain")

        queue_name = f"queue-{short_uid()}"
        queue_url = sqs_create_queue(QueueName=queue_name)

        assert_host_customisation(queue_url, use_localstack_cloud=True)
        assert queue_name in queue_url

    def test_path_strategy(self, monkeypatch, sqs_create_queue, assert_host_customisation):
        monkeypatch.setattr(config, "SQS_ENDPOINT_STRATEGY", "path")

        queue_name = f"queue-{short_uid()}"
        queue_url = sqs_create_queue(QueueName=queue_name)

        assert_host_customisation(queue_url, use_localhost=True)
        assert queue_name in queue_url


class TestLambda:
    @pytest.mark.skipif(condition=is_old_provider(), reason="Not implemented for legacy provider")
    def test_function_url(self, assert_host_customisation, lambda_client, create_lambda_function):
        function_name = f"function-{short_uid()}"
        handler_code = ""
        handler_file = new_tmp_file()
        save_file(handler_file, handler_code)

        create_lambda_function(
            func_name=function_name,
            handler_file=handler_file,
            runtime=Runtime.python3_9,
        )

        function_url = lambda_client.create_function_url_config(
            FunctionName=function_name,
            AuthType="NONE",
        )["FunctionUrl"]

        assert_host_customisation(function_url, use_localstack_cloud=True)

    @pytest.mark.skipif(condition=is_new_provider(), reason="Not implemented for new provider")
    def test_http_api_for_function_url(
        self, assert_host_customisation, create_lambda_function, aws_http_client_factory
    ):
        function_name = f"function-{short_uid()}"
        handler_code = ""
        handler_file = new_tmp_file()
        save_file(handler_file, handler_code)

        create_lambda_function(
            func_name=function_name,
            handler_file=handler_file,
            runtime=Runtime.python3_9,
        )

        client = aws_http_client_factory("lambda", signer_factory=SigV4Auth)
        url = f"/2021-10-31/functions/{function_name}/url"
        r = client.post(
            url,
            data=json.dumps(
                {
                    "AuthType": "NONE",
                }
            ),
            params={"Qualifier": "$LATEST"},
        )
        r.raise_for_status()

        function_url = r.json()["FunctionUrl"]

        assert_host_customisation(function_url, use_localstack_cloud=True)
