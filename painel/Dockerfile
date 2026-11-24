FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir flask==3.1.0 gunicorn==23.0.0
COPY app.py .
COPY templates templates
EXPOSE 5000
CMD ["gunicorn", "-b", "0.0.0.0:5000", "-w", "2", "--timeout", "30", "app:app"]
