"""End-to-end test: drive VRGDG_VideoBuilderMCP with the official MCP client
over stdio, calling tools against the live 8188 instance."""
import asyncio, os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "VRGDG_VideoBuilderMCP.py")
PY = sys.argv[1] if len(sys.argv) > 1 else sys.executable


async def main():
    params = StdioServerParameters(command=PY, args=[SERVER])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = [t.name for t in tools.tools]
            print("tool count:", len(names))
            print("tools:", names)

            res = await session.call_tool("list_projects", {})
            text = res.content[0].text if res.content else ""
            import json
            data = json.loads(text)
            print("list_projects ok:", data.get("ok"), "| projects:", len(data.get("projects", [])))

            # create a project through the MCP tool
            c = await session.call_tool("create_project", {"project_name": "MCP-E2E-TEST"})
            cd = json.loads(c.content[0].text)
            print("create_project ok:", cd.get("ok"), "| id:", (cd.get("data") or {}).get("id"))
            pid = (cd.get("data") or {}).get("id")

            # add a scene
            a = await session.call_tool("add_scene", {"project_id": pid, "scene": {"label": "S1", "prompt": "x"}, "base_revision": 0})
            ad = json.loads(a.content[0].text)
            print("add_scene ok:", ad.get("ok"), "| rev:", ad.get("revision"))

            # render dry-run
            r = await session.call_tool("render_project", {"project_id": pid, "dry_run": True})
            rid = json.loads(r.content[0].text)["data"]["id"]
            import time
            for _ in range(10):
                await asyncio.sleep(0.5)
                g = await session.call_tool("get_job", {"job_id": rid})
                gd = json.loads(g.content[0].text)["data"]
                if gd["status"] in ("complete", "failed"):
                    break
            print("render dry-run:", gd["status"], "|", gd["message"], "|", str(gd["error"])[:60])

            # cleanup
            d = await session.call_tool("delete_project", {"project_id": pid})
            print("delete ok:", json.loads(d.content[0].text).get("ok"))


asyncio.run(main())
