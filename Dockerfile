# 1. 使用官方的 Playwright Python 镜像作为基础
# 这已经包含了所有运行 Chromium 所需的系统依赖，以及一个特定版本的 Python
FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

# 2. 在容器内部创建一个工作目录
WORKDIR /app

# 3. 复制依赖文件并安装依赖
# 这一步利用了 Docker 的缓存机制，如果 requirements.txt 没有变化，就不会重新安装
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 4. 安装 Playwright 的浏览器二进制文件
# 我们只需要 chromium，并且不需要系统依赖，因为基础镜像已经有了
RUN playwright install chromium

# 5. 复制项目的所有其他文件到工作目录
COPY . .

# 6. 声明服务将要监听的端口
EXPOSE 10000

# 7. 定义容器启动时要运行的命令
# 直接使用 uvicorn 启动，并监听所有网络接口
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "10000"]
