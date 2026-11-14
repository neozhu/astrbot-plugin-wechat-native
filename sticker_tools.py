"""Small JSON tool boundary; sticker scope comes only from the trusted event."""

import asyncio

from .tool_support import context, encode, error_result


async def run_sticker(event, action, **kwargs):
    task = asyncio.current_task()
    adapter = None
    try:
        ctx = context(event, stickers=True, sticker_recovery=action == "send")
        adapter = ctx.adapter
        adapter._send_tasks.add(task)
        return encode(await getattr(adapter.stickers, action)(event, **kwargs))
    except Exception as error:
        return encode(error_result(action, error))
    finally:
        if adapter:
            adapter._send_tasks.discard(task)
