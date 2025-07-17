import asyncio
import base64
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
import uvicorn

from playwright.async_api import async_playwright
from openai import AsyncOpenAI

logging.basicConfig(level=logging.INFO, format='%(asctime)s - [%(levelname)s] - %(message)s')

COOKIE_FILE = '来客.json'
TARGET_URL = "https://www.life-data.cn/?channel_id=laike_data_first_menu&groupid=1768205901316096"
DEBUG_SCREENSHOTS_DIR = Path("debug_screenshots")
REFRESH_INTERVAL_SECONDS = 10

client = AsyncOpenAI(
    base_url='https://api-inference.modelscope.cn/v1/',
    api_key='bae85abf-09f0-4ea3-9228-1448e58549fc',
)
MODEL_ID = 'Qwen/Qwen2.5-VL-72B-Instruct' 

app_state = {"latest_data": None, "status": "Initializing..."}

# ============== START: 核心提示词优化 ==============
def get_detailed_prompt():
    return """
    你是一个极其严谨细致的数据提取助手。请分析这张仪表盘截图，并提取所有关键指标卡片的信息。

    你的回答必须是也只能是一个严格的JSON对象，不要添加任何额外的解释或Markdown标记。
    必须严格遵循以下JSON格式：
    { "update_time": "...", "comparison_date": "...", "metrics": [ { "name": "...", "value": "...", "comparison": "...", "status": "..." } ] }

    **至关重要的指令：**

    1.  **目标指标**: 只提取以下四个指标：`成交金额`、`核销金额`、`商品访问人数`、`核销券数`。
    2.  **数据准确性是最高优先级**:
        * **如果某个指标的数值在图片中清晰可见，请准确提取。**
        * **如果某个指标的数值看不见、被遮挡、或者其区域正在加载中（例如显示空白或加载动画），你必须为该指标的 `value` 字段返回一个空字符串 `""`。**
        * **在任何情况下，都绝对不允许猜测、估算、编造或补全任何数字。宁愿留空，也不能出错。**
        * **错误示例**: 图片中“核销券数”未加载，返回 `{"name": "核销券数", "value": "1234", ...}` -> 这是错误的！
        * **正确示例**: 图片中“核销券数”未加载，应返回 `{"name": "核销券数", "value": "", "comparison": "", "status": ""}`。
    3.  **忽略其他**: 忽略 “退款金额” 以及任何其他未在目标列表中指定的指标。
    """
# ============== END: 核心提示词优化 ==============

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
        logging.info(f"VLM 原始返回内容: {raw_content}")

        if raw_content.startswith("```json"): raw_content = raw_content[7:-3].strip()
        
        data = json.loads(raw_content)

        if "metrics" in data and isinstance(data["metrics"], list):
            valid_metrics = []
            for metric in data["metrics"]:
                value_str = metric.get("value", "")
                numeric_part = "".join(filter(lambda x: x in '0123456789.', value_str))
                if numeric_part and numeric_part != '.':
                    try:
                        float(numeric_part)
                        valid_metrics.append(metric)
                    except ValueError:
                        logging.warning(f"过滤无效指标: {metric['name']} 的值 '{value_str}' 不是有效数字。")
                        continue
                else:
                    logging.warning(f"过滤无效指标: {metric['name']} 的值 '{value_str}' 不含数字或为空。")
            data["metrics"] = valid_metrics

        return data
    except Exception as e:
        logging.error(f"调用视觉模型或解析JSON时出错: {e}")
        return {}

async def run_playwright_scraper():
    DEBUG_SCREENSHOTS_DIR.mkdir(exist_ok=True)
    logging.info(f"调试截图将保存在: '{DEBUG_SCREENSHOTS_DIR.resolve()}'")

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
            logging.info(f"正在导航到: {TARGET_URL}")
            await page.goto(TARGET_URL, wait_until="load", timeout=60000)
            logging.info("页面首次加载完成。")

            while True:
                logging.info("开始新一轮数据刷新...")
                await page.reload(wait_until="load", timeout=30000)
                logging.info("页面已重新加载。")
                key_element_selector = "div:has-text('成交金额')"
                logging.info(f"正在等待关键元素 '{key_element_selector}' 出现...")
                await page.wait_for_selector(key_element_selector, timeout=30000)
                logging.info("关键元素已找到，数据已渲染。")
                await asyncio.sleep(2)

                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                screenshot_path = DEBUG_SCREENSHOTS_DIR / f"screenshot_{timestamp}.png"

                logging.info(f"正在截取屏幕到: {screenshot_path}")
                await page.screenshot(path=screenshot_path, full_page=True)
                
                image_base64 = encode_image_to_base64(str(screenshot_path))
                
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
    print(f"\n      ➡️   [http://0.0.0.0:7860](http://0.0.0.0:7860)")
    print("="*60 + "\n")
    uvicorn.run(app, host="0.0.0.0", port=7860)
