"""P7.1/P7.3 — the UNAMBIGUOUS fail-open: LLM egress endpoint is unvalidated.

Plan v3.3 §7.1 + §6 (decision 6): the genuine fail-open is the un-applied
url_security blocklist on the LLM egress path. THREAT_MODEL.md:77 says PR #1039
fixed base_url SSRF for chat, but the LLM completion path still passes a
caller/token-supplied `endpoint_url` straight into `llm_call_async` with NO
validation:
    src/agent_loop.py:2215  raw = await llm_call_async(url=endpoint_url, ...)
    src/agent_loop.py:3164  ... endpoint_url=endpoint_url ...
    src/llm_core.py:1074    async def llm_call_async(url, ...)   # no url check
    src/llm_core.py:1201    async def stream_llm(url, ...)       # no url check

So an attacker who can influence `endpoint_url` (via a connector/token config)
can point the backend at 169.254.169.254 (IMDS) or 10.x and exfiltrate the
prompt + steal cloud creds — the REAL exfil path the plan calls out.

DESIRED behaviour (lands in P6.2): the internal-facing llm_core chokepoint
applies the EXISTING url_security blocklist to outbound model URLs (admin-
allowlisted private providers excepted by config, not by bypassing the guard).

These tests assert that desired behaviour and are RED-until-P6.2.
"""
import pytest

from src.url_security import validate_public_http_url


def test_blocklist_primitive_rejects_imds_and_private_ranges():
    """The primitive already exists and works — P6.2 only has to CALL it on the
    egress path. This proves the building block so the gap is purely wiring."""
    for bad in (
        "http://169.254.169.254/latest/meta-data/",   # cloud IMDS
        "http://10.0.0.5:8000/v1/chat/completions",     # RFC1918
        "http://127.0.0.1:11434/v1/chat/completions",   # loopback
    ):
        with pytest.raises(ValueError):
            validate_public_http_url(bad)


@pytest.mark.skip(
    reason="RED-until-P6.2: llm_core egress does not yet validate endpoint_url "
           "(agent_loop.py:2215/3164, llm_core.py:1074/1201). Remove skip when "
           "P6.2 wires url_security into the LLM egress chokepoint."
)
def test_llm_call_async_rejects_blocked_endpoint_url():
    """When P6.2 lands, llm_call_async must refuse a blocked egress URL before
    making any outbound request."""
    import asyncio
    from src.llm_core import llm_call_async

    with pytest.raises(ValueError):
        asyncio.run(llm_call_async(
            url="http://169.254.169.254/v1/chat/completions",
            model="x",
            messages=[{"role": "user", "content": "hi"}],
        ))


@pytest.mark.skip(
    reason="RED-until-P6.2: stream_llm egress does not yet validate the URL."
)
def test_stream_llm_rejects_blocked_endpoint_url():
    import asyncio
    from src.llm_core import stream_llm

    async def _drive():
        async for _ in stream_llm(
            url="http://10.0.0.9/v1/chat/completions",
            model="x",
            messages=[{"role": "user", "content": "hi"}],
        ):
            pass

    with pytest.raises(ValueError):
        asyncio.run(_drive())
