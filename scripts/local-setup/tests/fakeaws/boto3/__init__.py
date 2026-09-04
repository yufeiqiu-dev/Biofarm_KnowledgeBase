"""Minimal boto3 stand-in for exercising provision_aws.py without an AWS account.

Records every call so a test can assert on the arguments the script sends.
"""
from botocore.exceptions import ClientError

CALLS = []

# Set to a method name to make that call raise ExpiredToken, so the
# short-lived-credential path (ada, SSO, assume-role) can be exercised.
RAISE_EXPIRED_ON = None


def _err(code):
    return ClientError({"Error": {"Code": code}})


class _Paginator:
    def __init__(self, pages):
        self._pages = pages

    def paginate(self, **kwargs):
        return iter(self._pages)


class _Client:
    def __init__(self, name):
        self._name = name

    def __getattr__(self, method):
        def call(**kwargs):
            CALLS.append((self._name, method, kwargs))
            return self._respond(method, kwargs)
        return call

    def can_paginate(self, method):
        return True

    def get_paginator(self, method):
        CALLS.append((self._name, f"paginate:{method}", {}))
        empty = {
            "list_distributions": [{"DistributionList": {"Items": []}}],
            "list_user_pools": [{"UserPools": []}],
            "list_user_pool_clients": [{"UserPoolClients": []}],
            "list_object_versions": [{}],
        }
        return _Paginator(empty.get(method, [{}]))

    def _respond(self, method, kwargs):
        n, m = self._name, method
        if RAISE_EXPIRED_ON and m == RAISE_EXPIRED_ON:
            raise _err("ExpiredToken")
        if (n, m) == ("sts", "get_caller_identity"):
            return {"Account": "123456789012", "Arn": "arn:aws:iam::123456789012:user/tester"}
        if (n, m) == ("s3", "head_bucket"):
            raise _err("404")
        if (n, m) == ("s3", "create_bucket"):
            return {}
        if (n, m) == ("cloudfront", "list_origin_access_controls"):
            return {"OriginAccessControlList": {"Items": []}}
        if (n, m) == ("cloudfront", "create_origin_access_control"):
            return {"OriginAccessControl": {"Id": "OAC123"}}
        if (n, m) == ("cloudfront", "create_distribution"):
            return {"Distribution": {"Id": "E1FAKE", "DomainName": "d123fake.cloudfront.net"}}
        if (n, m) == ("iam", "get_user"):
            raise _err("NoSuchEntity")
        if (n, m) == ("iam", "list_access_keys"):
            return {"AccessKeyMetadata": []}
        if (n, m) == ("iam", "create_access_key"):
            return {"AccessKey": {"AccessKeyId": "AKIAFAKE", "SecretAccessKey": "s3cr3t-fake"}}
        if (n, m) == ("cognito-idp", "create_user_pool"):
            return {"UserPool": {"Id": "us-east-2_FAKE123"}}
        if (n, m) == ("cognito-idp", "create_user_pool_client"):
            return {"UserPoolClient": {"ClientId": "clientfake123"}}
        if (n, m) == ("cognito-idp", "describe_user_pool"):
            return {"UserPool": {"Id": "us-east-2_FAKE123"}}  # no Domain yet
        return {}


class Session:
    def __init__(self, profile_name=None):
        self.profile_name = profile_name

    def client(self, name, region_name=None):
        return _Client(name)
