class ClientError(Exception):
    def __init__(self, response, operation_name="Op"):
        self.response = response
        self.operation_name = operation_name
        super().__init__(str(response))


class NoCredentialsError(Exception):
    pass
