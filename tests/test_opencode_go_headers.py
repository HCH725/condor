"""OpenCode Go requires stable session routing, not a generic SDK identity."""

import asyncio

from condor.acp.pydantic_ai_client import PydanticAIClient


def test_go_headers_are_stable_per_client_and_isolated_between_clients():
    async def check():
        client = PydanticAIClient(
            "custom@Go:deepseek-v4.1-flash",
            base_url="https://opencode.ai/inference/go/v1",
            api_key="test-key",
        )
        other = PydanticAIClient(
            "custom@Go:deepseek-v4.1-flash",
            base_url="https://opencode.ai/inference/go/openai/v1",
            api_key="test-key",
        )
        models = [
            await client._build_model(),
            await client._build_model(),
            await other._build_model(),
        ]
        try:
            headers = [m.client.default_headers for m in models]
            session = headers[0].get("x-opencode-session")
            assert session, "Go rejects requests without x-opencode-session"
            assert session == headers[1].get("x-opencode-session")
            assert session != headers[2].get("x-opencode-session")
            assert all(h.get("User-Agent") == "condor/0.1.0" for h in headers)
        finally:
            for model in models:
                await model.client.close()

    asyncio.run(check())


def test_go_headers_do_not_leak_to_other_custom_endpoints():
    async def check():
        for base in (
            "https://example.com/v1",
            "https://opencode.ai/inference/v1",
            "https://opencode.ai.example.com/inference/go/v1",
        ):
            client = PydanticAIClient(
                "custom@Other:model", base_url=base, api_key="test-key"
            )
            model = await client._build_model()
            try:
                assert "x-opencode-session" not in model.client.default_headers
            finally:
                await model.client.close()

    asyncio.run(check())
