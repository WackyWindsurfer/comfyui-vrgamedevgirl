"""Real end-to-end generation run through the MCP server (the exact agentic path
Hermes Desktop uses): create project -> add scene -> generate_scene (flux_klein,
real models) -> poll get_job -> verify the image file exists on disk -> cleanup."""
import asyncio, os, sys, json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mcp import ClientSession
from mcp.client.stdio import stdio_client, StdioServerParameters

SERVER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "VRGDG_VideoBuilderMCP.py")
PY = sys.argv[1] if len(sys.argv) > 1 else sys.executable

# Real models from the shared folder E:\AI\models (visible to the 8188 instance).
# Krea2 (proven working set): UNET Krea2-Turbo-int8-ConvRot + CLIP type krea2 +
# Qwen image VAE. The UNET file lives in the Krea2/ subfolder, so the live
# instance enumerates it with a subfolder prefix.
UNET = "Krea2\\Krea2-Turbo-int8-ConvRot.safetensors"
CLIP = "qwen3vl_4b_fp8_scaled.safetensors"
VAE = "qwen_image_vae.safetensors"
BUILDER = "krea2"


def tool(res):
    return json.loads(res.content[0].text)


async def main():
    params = StdioServerParameters(command=PY, args=[SERVER])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as s:
            await s.initialize()

            c = tool(await s.call_tool("create_project", {"project_name": "REAL-E2E-GEN"}))
            pid = c["data"]["id"]
            print("created:", pid, "| rev:", c.get("revision"))

            a = tool(await s.call_tool("add_scene", {"project_id": pid, "base_revision": c.get("revision", 0),
                                                    "scene": {"label": "S1", "prompt": "a red cube on a wooden table, soft light"}}))
            print("add_scene ok:", a.get("ok"), "| rev:", a.get("revision"))
            scene_id = (a.get("data") or {}).get("scene", {}).get("id") or "1"

            g = tool(await s.call_tool("generate_scene", {
                "project_id": pid, "scene_id": scene_id, "builder": BUILDER,
                "collect": "image", "timeout_s": 600,
                "builder_payload": {"prompt": "a red cube on a wooden table, soft light",
                                   "krea_unet_name": UNET, "krea_clip_name": CLIP,
                                   "krea_vae_name": VAE,
                                   "z_unet_name": "z_image_turbo_int8_convrot.safetensors",
                                   "z_clip_name": "qwen_3_4b.safetensors",
                                   "width": 512, "height": 512, "seed": 1234}}))
            job_id = g["data"]["id"]
            print("generate job:", job_id, "| status:", g["data"]["status"])

            # poll
            status = None
            for i in range(200):
                await asyncio.sleep(3)
                j = tool(await s.call_tool("get_job", {"job_id": job_id}))["data"]
                status = j["status"]
                if i % 10 == 0:
                    print(f"  [{i*3}s] {status} | {j.get('message')}")
                if status in ("complete", "failed", "cancelled"):
                    break
            print("final job status:", status)
            print("result:", json.dumps(j.get("result"))[:300])
            print("error:", str(j.get("error"))[:300])

            # verify the image file exists on disk
            img = (j.get("result") or {}).get("image") or {}
            if isinstance(img, dict):
                fname = img.get("filename")
                folder = img.get("folder")
                sub = img.get("subfolder", "")
                if fname:
                    p = os.path.join(os.environ.get("COMFYUI_OUT", "D:/AI/ComfyUI-Installs/HERMES/ComfyUI/output"), folder or "", sub, fname)
                    print("image file exists:", os.path.isfile(p), "|", p)

            # cleanup
            d = tool(await s.call_tool("delete_project", {"project_id": pid}))
            print("delete ok:", d.get("ok"))


asyncio.run(main())
