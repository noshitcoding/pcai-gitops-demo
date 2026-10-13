FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 \
    HOME=/tmp \
    GRADIO_TEMP_DIR=/tmp/gradio \
    GRADIO_ANALYTICS_ENABLED=False
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py VERSION ./
# AI Essentials sets the project UID at runtime; the app only writes to /tmp.
USER 10001
EXPOSE 7860
CMD ["python", "app.py"]
