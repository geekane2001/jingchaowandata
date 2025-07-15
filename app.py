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

# --- 全局配置 ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - [%(levelname)s] - %(message)s')
COOKIE_FILE = '来客.json'
TARGET_URL = "https://www.life-data.cn/?channel_id=laike_data_first_menu&groupid=1768205901316096"
SCREENSHOT_PATH = "dashboard_screenshot.png"
DEBUG_SCREENSHOT_PATH = "debug_timeout.png"
REFRESH_INTERVAL_SECONDS = 15
API_KEY = "bae85abf-09f0-4ea3-9228-1448e58549fc"

client = AsyncOpenAI(base_url='https://api-inference.modelscope.cn/v1/', api_key=API_KEY)
MODEL_ID = 'Qwen/Qwen2.5-VL-7B-Instruct' 
app_state = {"task": None, "latest_data": None, "status": "Initializing..."}

# ... get_detailed_prompt, encode_image_to_base64, analyze_image_with_vlm, wait_for_data_to_load 保持不变 ...
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
        logging.info("✔ 关键数据已加载，页面准备就绪！")
        return True
    except PlaywrightTimeoutError:
        logging.error(f"智能等待超时({timeout/1000}s)：关键数据未能加载。")
        return False

# =========================================================
# === 终极版弹窗处理函数 ===
# =========================================================
async def handle_onboarding_popups(page: Page):
    logging.info("--- 开始处理引导弹窗（终极版） ---")

    # 辅助函数：查找并点击弹窗内的第一个匹配按钮
    async def find_and_click_first(button_text: str, timeout: int = 3000):
        logging.info(f"   - 正在查找 '{button_text}' 按钮...")
        try:
            # 定位所有可见的、包含指定文本的按钮
            # 我们不限制它必须在弹窗内，以增加通用性
            button_locator = page.get_by_text(button_text, exact=True)
            
            # 等待至少一个这样的按钮变得可见
            await button_locator.first.wait_for(state="visible", timeout=timeout)
            
            logging.info(f"   ✔ [检测成功] 发现可见的 '{button_text}' 按钮，准备点击第一个。")
            await button_locator.first.click(force=True)
            logging.info(f"   ✔ [点击成功] 已点击 '{button_text}' 按钮。")
            await page.wait_for_timeout(1500) # 点击后等待UI响应
        except Exception:
            logging.info(f"   - [未检测到] 未在 {timeout}ms 内发现 '{button_text}' 按钮，跳过。")

    # 严格按照顺序处理引导流程
    await find_and_click_first("知道了")
    await find_and_click_first("下一步")

    # 对“去体验”按钮使用更特殊的处理逻辑
    try:
        logging.info("   - 正在特殊处理 '去体验' 按钮...")
        go_experience_locator = page.get_by_text("去体验", exact=True).first
        await go_experience_locator.wait_for(state="visible", timeout=3000)
        logging.info("   ✔ [检测成功] 发现 '去体验' 按钮，使用dispatch_event点击。")
        await go_experience_locator.dispatch_event('click')
        logging.info("   ✔ [点击成功] 已通过dispatch_event点击 '去体验' 按钮。")
        await page.wait_for_timeout(1500)
    except Exception:
        logging.info("   - [未检测到] 未发现 '去体验' 按钮，跳过。")

    logging.info("--- 引导弹窗处理完毕 ---")


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
            await page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=90000)
            
            # 在第一次加载后，处理引导弹窗
            await handle_onboarding_popups(page)

            while True:
                try:
                    logging.info("开始新一轮数据刷新...")
                    await page.reload(wait_until="domcontentloaded", timeout=90000)
                    
                    # 每次刷新后，都检查一下是否还有弹窗
                    await handle_onboarding_popups(page)
                    
                    logging.info("正在滚动到页面顶部...")
                    await page.evaluate("() => window.scrollTo(0, 0)")
                    await page.wait_for_timeout(500)

                    if await wait_for_data_to_load(page):
                        logging.info("准备进行最终截图...")
                        await page.screenshot(path=SCREENSHOT_PATH, full_page=True)
                        image_base64 = encode_image_to_base64(SCREENSHOT_PATH)
                        if image_base64:
                            analysis_result = await analyze_image_with_vlm(image_base64)
                            if analysis_result and analysis_result.get('metrics'):
                                app_state["latest_data"] = analysis_result
                                app_state["status"] = f"数据已更新。下一次刷新在 {REFRESH_INTERVAL_SECONDS} 秒后。"
                            else:
                                app_state["status"] = "AI分析未能从截图中提取有效数据。"
                        else:
                            app_state["status"] = "创建截图失败。"
                    else:
                        app_state["status"] = "处理引导后，数据仍加载超时。"
                        await page.screenshot(path=DEBUG_SCREENSHOT_PATH, full_page=True)

                except Exception as e:
                    logging.error(f"后台任务循环发生错误: {e}")
                    app_state["status"] = "后台任务发生错误，正在重试..."
                    await page.screenshot(path=DEBUG_SCREENSHOT_PATH, full_page=True)
                
                await asyncio.sleep(REFRESH_INTERVAL_SECONDS)
        
        except PlaywrightTimeoutError as e:
            app_state["status"] = "致命错误: 无法打开目标页面，请检查Cookie是否有效。"
            logging.error(f"首次导航失败，任务终止: {e}", exc_info=True)
            await page.screenshot(path=DEBUG_SCREENSHOT_PATH, full_page=True)
        finally:
            await browser.close()

# ... lifespan 和 FastAPI 应用定义保持不变 ...
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

app = FastAPI(lifespan=lifespan_manager)
@app.get("/data")
async def get_data():
    if app_state["latest_data"] is None: return {"status": app_state["status"], "data": None}
    return {"status": app_state["status"], "data": app_state["latest_data"]}
@app.get("/debug_screenshot")
async def get_debug_screenshot():
    if os.path.exists(DEBUG_SCREENSHOT_PATH): return FileResponse(DEBUG_SCREENSHOT_PATH)
    return HTTPException(status_code=404, detail="调试截图不存在。")

app.mount("/", StaticFiles(directory=".", html=True), name="static")

if __name__ == "__main__":
    print("\n" + "="*60); print("      🚀 竞潮玩实时数据看板 (终极交互版) 🚀"); print(f"\n      ➡️   http://127.0.0.1:7860"); print("="*60 + "\n")
    uvicorn.run(app, host="127.0.0.1", port=7860)
