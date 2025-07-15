import asyncio
import base64
import json
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
import uvicorn

from playwright.async_api import async_playwright
from openai import AsyncOpenAI

logging.basicConfig(level=logging.INFO, format='%(asctime)s - [%(levelname)s] - %(message)s')

COOKIE_FILE = '来客.json'
TARGET_URL = "https://www.life-data.cn/?channel_id=laike_data_first_menu&groupid=1768205901316096"
SCREENSHOT_PATH = "dashboard_screenshot.png"
REFRESH_INTERVAL_SECONDS = 10

client = AsyncOpenAI(
    base_url='https://api-inference.modelscope.cn/v1/',
    api_key='bae85abf-09f0-4ea3-9228-1448e58549fc',
)
MODEL_ID = 'Qwen/Qwen2.5-VL-72B-Instruct' 

app_state = {"latest_data": None, "status": "Initializing..."}

def get_detailed_prompt():
    return """
    你是一个专业的数据分析师。请分析这张仪表盘截图，并提取所有关键指标卡片的信息。
    严格按照以下JSON格式返回，不要添加任何额外的解释或Markdown标记。
    { "update_time": "...", "comparison_date": "...", "metrics": [ { "name": "...", "value": "...", "comparison": "...", "status": "..." } ] }
    请确保：
    1. **只提取以下指标**：成交金额、核销金额、商品访问人数、核销券数。
    2. **忽略“退款金额”** 以及其他所有未列出的指标。
    3. 所有字段都从图片中准确提取。
    """

def encode_image_to_base64(image_path: str) -> str:
    try:
        with open(image_path, "rb") as image_file:
            return base64.b64encode(image_file.read()).decode('utf-8')
    except FileNotFoundError: return ""

async def analyze_image_with_vlm(image_base64: str) -> dict:
    if not image_base64: return {}
    try:
        response = await client.chat.completions.create(
            model=MODEL_ID,
            messages=[{
                'role': 'user',
                'content': [
                    {'type': 'text', 'text': get_detailed_prompt()},
                    {'type': 'image_url', 'image_url': {'url': f'data:image/png;base64,{image_base64}'}}
                ],
            }]
        )
        raw_content = response.choices[0].message.content
        if raw_content.startswith("```json"): raw_content = raw_content[7:-3].strip()
        return json.loads(raw_content)
    except Exception as e:
        logging.error(f"调用视觉模型或解析JSON时出错: {e}")
        return {}

async def run_playwright_scraper():
    if not os.path.exists(COOKIE_FILE):
        app_state["status"] = f"错误: Cookie 文件 '{COOKIE_FILE}' 未找到。"
        logging.error(app_state["status"])
        return
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        try:
            with open(COOKIE_FILE, 'r', encoding='utf-8') as f:
                await context.add_cookies(json.load(f)['cookies'])
        except Exception as e:
            app_state["status"] = f"加载 Cookie 失败: {e}"
            logging.error(app_state["status"])
            await browser.close()
            return
            
        page = await context.new_page()
        try:
            # 首次加载页面
            logging.info(f"正在导航到: {TARGET_URL}")
            await page.goto(TARGET_URL, wait_until="load", timeout=60000)
            logging.info("页面首次加载完成。")

            while True:
                logging.info("开始新一轮数据刷新...")
                
                # --- START: 核心修改区域 ---
                
                # 步骤 1: 重新加载页面，但使用 'load' 替代 'networkidle'
                # 'load' 事件在页面主要资源加载后即触发，不会被实时数据请求阻塞
                await page.reload(wait_until="load", timeout=30000)
                logging.info("页面已重新加载。")

                # 步骤 2: 等待一个关键的数据元素出现，这标志着仪表盘已渲染
                # 我们选择等待包含“成交金额”文本的元素，这是一个可靠的指标
                # 你需要用浏览器的开发者工具检查目标页面，找到一个最合适的选择器
                key_element_selector = "div:has-text('成交金额')"
                logging.info(f"正在等待关键元素 '{key_element_selector}' 出现...")
                await page.wait_for_selector(key_element_selector, timeout=30000)
                logging.info("关键元素已找到，数据已渲染。")

                # 步骤 3 (可选，但推荐): 添加一个短暂的固定延时，以确保动画或最终渲染完成
                await asyncio.sleep(2) # 等待2秒
                
                # --- END: 核心修改区域 ---

                logging.info("正在截取屏幕...")
                await page.screenshot(path=SCREENSHOT_PATH, full_page=True)
                
                image_base64 = encode_image_to_base64(SCREENSHOT_PATH)
                if image_base64:
                    logging.info("截图成功，正在发送给AI进行分析...")
                    analysis_result = await analyze_image_with_vlm(image_base64)
                    if analysis_result:
                        app_state["latest_data"] = analysis_result
                        app_state["status"] = f"数据已更新。下一次刷新在 {REFRESH_INTERVAL_SECONDS} 秒后。"
                        logging.info("AI分析完成，数据已更新。")
                    else:
                        app_state["status"] = "AI分析未能生成有效数据，正在重试..."
                        logging.warning(app_state["status"])
                else:
                    app_state["status"] = "创建截图失败，正在重试..."
                    logging.warning(app_state["status"])
                    
                logging.info(f"等待 {REFRESH_INTERVAL_SECONDS} 秒后进行下一次刷新。")
                await asyncio.sleep(REFRESH_INTERVAL_SECONDS)
        except Exception as e:
            app_state["status"] = f"主循环发生严重错误: {e}"
            logging.error(f"主循环异常: {e}", exc_info=True)
        finally:
            await browser.close()
            
@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(run_playwright_scraper())
    yield

app = FastAPI(lifespan=lifespan)

@app.get("/data")
async def get_data():
    if app_state["latest_data"] is None:
        raise HTTPException(status_code=404, detail={"status": app_state["status"], "data": None})
    return {"status": app_state["status"], "data": app_state["latest_data"]}

app.mount("/", StaticFiles(directory=".", html=True), name="static")

if __name__ == "__main__":
    print("\n" + "="*60)
    print("      🚀 竞潮玩实时数据看板 🚀")
    print(f"\n      ➡️   http://0.0.0.0:7860")
    print("="*60 + "\n")
    uvicorn.run(app, host="0.0.0.0", port=7860)
