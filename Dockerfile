FROM python:3.12-slim
WORKDIR /app
COPY auth.py network.py server.py storage.py tools.py plan.html consent.html admin.py ./
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 BIND_HOST=0.0.0.0 PORT=8765 DATA_DIR=/data
RUN useradd --uid 10001 --create-home app && mkdir /data && chown app:app /data
USER app
EXPOSE 8765
CMD ["python", "server.py"]
