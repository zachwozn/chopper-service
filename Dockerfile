FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY chopper_service.py .
COPY data /data
EXPOSE 8000
CMD ["uvicorn", "chopper_service:app", "--host", "0.0.0.0", "--port", "8000"]
