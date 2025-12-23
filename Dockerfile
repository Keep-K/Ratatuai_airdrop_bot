FROM python:3.12-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# airdrop-bot 폴더의 파일들을 복사
COPY airdrop-bot/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY airdrop-bot/ /app/

CMD ["python", "bot.py"]

