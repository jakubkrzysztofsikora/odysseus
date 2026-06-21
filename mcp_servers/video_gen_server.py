"""
video_gen_server.py

MCP server exposing video generation via OpenAI-compatible APIs.
"""

import asyncio
import sys
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

server = Server("video_gen")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="generate_video",
            description="Generate a video using a video-capable model (e.g. seedance)",
            inputSchema={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "Video description prompt"},
                    "duration": {"type": "string", "description": "Duration in seconds: 5 or 10 (default 5)"},
                    "resolution": {"type": "string", "description": "Resolution: 720p or 1080p (default 720p)"},
                    "aspect_ratio": {"type": "string", "description": "Aspect ratio (e.g. 16:9, 9:16)"},
                },
                "required": ["prompt"],
            },
        )
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name != "generate_video":
        return [TextContent(type="text", text=f"Unknown tool: {name}")]

    prompt = arguments.get("prompt", "")
    duration = arguments.get("duration", "")
    resolution = arguments.get("resolution", "")
    aspect_ratio = arguments.get("aspect_ratio", "")

    if not prompt:
        return [TextContent(type="text", text="Error: Video prompt is required")]

    try:
        from src.settings import get_setting
        from src.ai_interaction import do_generate_video

        if not get_setting("video_gen_enabled", True):
            return [TextContent(type="text", text="Error: Video generation is disabled by the administrator.")]

        # Build the newline content string per the do_generate_video contract:
        #   line1=prompt, line2=duration, line3=resolution, line4=aspect_ratio
        content = "\n".join([prompt, duration, resolution, aspect_ratio])

        result = await do_generate_video(content, owner=None)

        if isinstance(result, dict) and result.get("error"):
            return [TextContent(type="text", text=f"Error: {result['error']}")]

        video_url = result.get("video_url", "")
        # "Direct link:" rather than a "video_url:" label — small models copied the
        # label token into the link href, producing a broken link (mirrors image_gen).
        text = (
            f"Generated video for: {prompt[:100]}\n"
            f"Direct link: {video_url}\n"
            f"model: {result.get('video_model', '')}\n"
            f"size: {result.get('video_size', '')}"
        )
        return [TextContent(type="text", text=text)]

    except ValueError as e:
        return [TextContent(type="text", text=f"Error: {e}")]
    except Exception as e:
        return [TextContent(type="text", text=f"Error: {e}")]


async def run():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(run())
