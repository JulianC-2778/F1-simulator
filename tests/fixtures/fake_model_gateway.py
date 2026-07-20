class FakeModelGateway:
    def __init__(self, response: str = "fake response") -> None:
        self.response = response
        self.calls = []

    async def chat(self, messages, *, on_token=None):
        self.calls.append(messages)
        if on_token:
            await on_token(self.response)
        return self.response
