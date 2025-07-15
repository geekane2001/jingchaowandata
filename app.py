import asyncio
import base64
import json
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import uvicorn

from playwright.async_api import async_playwright, Page
from openai import AsyncOpenAI
from playwright._impl._errors import TimeoutError as PlaywrightTimeoutError

logging.basicConfig(level=logging.INFO, format='%(asctime)s - [%(levelname)s] - %(message)s')

# --- 路径配置 ---
TARGET_URL = "https://www.life-data.cn/?channel_id=laike_data_first_menu&groupid=1768205901316096"
COOKIE_FILE = '来客.json'
SCREENSHOT_PATH = "dashboard_screenshot.png"
# --- 调试截图路径 ---
GOTO_SCREENSHOT = "debug_goto.png"
RELOAD_SCREENSHOT = "debug_reload.png"
TIMEOUT_SCREENSHOT = "debug_timeout.png"

REFRESH_INTERVAL_SECONDS = 10
API_KEY = "bae85abf-09f0-4ea3-9228-1448e58549fc"

client = AsyncOpenAI(base_url='https://api-inference.modelscope.cn/v1/', api_key=API_KEY)
MODEL_ID = 'Qwen/Qwen2.5-VL-7B-Instruct' 
app_state = {"task": None, "latest_data": None, "status": "Initializing..."}

# ... get_detailed_prompt, encode_image_to_base64, analyze_image_with_vlm 保持不变 ...
def get_detailed_prompt():
    return """
    你是一个专业的数据分析师。请分析这张仪表盘截图，并提取所有关键指标卡片的信息。
    严格按照以下JSON格式返回，不要添加任何额外的解释或Markdown标记。
    { "update_time": "...", "comparison_date": "...", "metrics": [ { "name": "...", "value": "...", "comparison": "...", "status": "..." } ] }
    请确保：只提取成交金额、核销金额、商品访问人数、核销券数。忽略“退款金额”。
    """
def encode_image_to_base64(image_path: str) -> str:
    try:
        with open(image_path, "rb") as image_file: return base64.b64encode(image_file.read()).decode('utf-8')
    except FileNotFoundError: return ""
async def analyze_image_with_vlm(image_base64: str) -> dict:
    if not image_base64: return {}
    try:
        response = await client.chat.completions.create(model=MODEL_ID, messages=[{'role': 'user', 'content': [{'type': 'text', 'text': get_detailed_prompt()}, {'type': 'image_url', 'image_url': {'url': f'data:image/png;base64,{image_base64}'}}],}])
        raw_content = response.choices[0].message.content
        if raw_content.startswith("```json"): raw_content = raw_content[7:-3].strip()
        return json.loads(raw_content)
    except Exception as e: logging.error(f"调用视觉模型或解析JSON时出错: {e}"); return {}

async def wait_for_data_to_load(page: Page, timeout: int = 60000):
    logging.info("正在智能等待页面数据加载...")
    try:
        await page.wait_for_function("() => { const el = document.querySelector('div[class*=\"index-module_value_\"]'); return el && el.innerText.trim() !== '0' && el.innerText.trim() !== '--'; }", timeout=timeout)
        logging.info("关键数据已加载，页面准备就绪！")
        return True
    except PlaywrightTimeoutError:
        logging.error(f"智能等待超时({timeout/1000}s)：关键数据未能加载。")
        return False

async def run_playwright_scraper():
    await asyncio.sleep(5) 
    if not os.path.exists(COOKIE_FILE):
        app_state["status"] = f"错误: Cookie文件 '{COOKIE_FILE}' 在容器内未找到。"
        logging.error(app_state["status"]); return
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        try:
            with open(COOKIE_FILE, 'r', encoding='utf-8') as f:
                cookie_data = json.load(f); await context.add_cookies(cookie_data.get('cookies', []))
            logging.info(f"成功从 {COOKIE_FILE} 加载Cookie。")
        except Exception as e:
            app_state["status"] = f"从 {COOKIE_FILE} 加载或设置Cookie失败: {e}"; await browser.close(); return
        
        page = await context.new_page()
        try:
            # --- 步骤1：初次导航并截图 ---
            logging.info(f"正在导航至: {TARGET_URL}")
            await page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=90000)
            await page.screenshot(path=GOTO_SCREENSHOT, full_page=True)
            logging.info(f"首次导航完成，快照已保存至 {GOTO_SCREENSHOT}。")

            while True:
                try:
                    logging.info("开始新一轮数据刷新...")
                    # --- 步骤2：刷新页面并截图 ---
                    await page.reload(wait_until="domcontentloaded", timeout=90000)
                    await page.screenshot(path=RELOAD_SCREENSHOT, full_page=True)
                    logging.info(f"页面刷新完成，快照已保存至 {RELOAD_SCREENSHOT}。")

                    if await wait_for_data_to_load(page):
                        await page.screenshot(path=SCREENSHOT_PATH, full_page=True)
                        image_base64 = encode_image_to_base64(SCREENSHOT_PATH)
                        # ... (后续AI分析逻辑不变)
                        if image_base64:
                            analysis_result = await analyze_image_with_vlm(image_base64)
                            if analysis_result and analysis_result.get('metrics'): app_state["latest_data"] = analysis_result; app_state["status"] = f"数据已更新。下一次刷新在 {REFRESH_INTERVAL_SECONDS} 秒后。"
                            else: app_state["status"] = "AI分析未能从截图中提取有效数据。"
                        else: app_state["status"] = "创建截图失败。"
                    else:
                        # --- 步骤3：智能等待超时后截图 ---
                        app_state["status"] = "目标页面数据加载超时。"
                        await page.screenshot(path=TIMEOUT_SCREENSHOT, full_page=True)
                        logging.info(f"智能等待超时，快照已保存至 {TIMEOUT_SCREENSHOT}。")

                except PlaywrightTimeoutError as e:
                    # 如果是reload超时，也在这里截图
                    logging.error(f"页面刷新(reload)超时，正在保存当前快照...")
                    await page.screenshot(path=TIMEOUT_SCREENSHOT, full_page=True)
                    app_state["status"] = "目标页面加载超时，正在重试...";
                except Exception as e:
                    logging.error(f"后台任务循环发生错误: {e}")
                    app_state["status"] = "后台任务发生错误，正在重试...";
                    await page.screenshot(path=TIMEOUT_SCREENSHOT, full_page=True)
                
                await asyncio.sleep(REFRESH_INTERVAL_SECONDS)
        
        except PlaywrightTimeoutError as e:
            # 如果是首次goto超时
            app_state["status"] = "致命错误: 无法打开目标页面。";
            logging.error(f"首次导航失败，任务终止: {e}", exc_info=True)
            # 在goto失败的情况下，页面是空白的，截图意义不大但仍可保留
            try:
                await page.screenshot(path=GOTO_SCREENSHOT, full_page=True)
            except:
                pass
        finally:
            await browser.close()

@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.info("接收到 'lifespan.startup' 事件，正在启动后台抓取任务...")
    app_state["task"] = asyncio.create_task(run_playwright_scraper())
    yield
    logging.info("接收到 'lifespan.shutdown' 事件，正在取消后台任务...")
    if app_state["task"]:
        app_state["task"].cancel()
        try: await app_state["task"]
        except asyncio.CancelledError: logging.info("后台任务已成功取消。")

app = FastAPI(lifespan=lifespan)

@app.get("/data")
async def get_data():
    if app_state["latest_data"] is None: return {"status": app_state["status"], "data": None}
    return {"status": app_state["status"], "data": app_state["latest_data"]}

# --- 新增的调试路由 ---
@app.get("/debug_goto")
async def get_goto_screenshot():
    if os.path.exists(GOTO_SCREENSHOT): return FileResponse(GOTO_SCREENSHOT)
    return HTTPException(status_code=404, detail="首次导航快照不存在。")

@app.get("/debug_reload")
async def get_reload_screenshot():
    if os.path.exists(RELOAD_SCREENSHOT): return FileResponse(RELOAD_SCREENSHOT)
    return HTTPException(status_code=404, detail="刷新后快照不存在。")

@app.get("/debug_timeout")
async def get_timeout_screenshot():
    if os.path.exists(TIMEOUT_SCREENSHOT): return FileResponse(TIMEOUT_SCREENSHOT)
    return HTTPException(status_code=404, detail="超时快照不存在。")

app.mount("/", StaticFiles(directory=".", html=True), name="static")

if __name__ == "__main__":
    print("\n" + "="*60)
    print("      🚀 竞潮玩实时数据看板 (多点快照调试模式) 🚀")
    print(f"\n      ➡️   http://127.0.0.1:7860")
    print("="*60 + "\n")
    uvicorn.run(app, host="127.0.0.1", port=7860)
