# Railway 배포 가이드

## 방법 1: Railway CLI 사용 (터미널)

### 1. Railway 로그인
```bash
railway login
```

### 2. 프로젝트 초기화 및 배포
```bash
cd airdrop-bot
railway init
railway up
```

### 3. 환경 변수 설정
```bash
railway variables set BOT_TOKEN=your_bot_token_here
railway variables set BOT_USERNAME=YourBotUsernameWithoutAt
railway variables set RECIPE_AI_BASE_URL=https://recipe-ai-production.up.railway.app
railway variables set RECIPE_AI_TIMEOUT=20
railway variables set DB_PATH=/data/db.sqlite3
```

또는 한 번에 설정:
```bash
railway variables set BOT_TOKEN=your_token BOT_USERNAME=your_username RECIPE_AI_BASE_URL=https://recipe-ai-production.up.railway.app RECIPE_AI_TIMEOUT=20 DB_PATH=/data/db.sqlite3
```

### 4. 배포 확인
```bash
railway status
railway logs
```

## 방법 2: GitHub 연동 (웹 UI)

1. Railway 웹사이트 (https://railway.app) 접속
2. "New Project" 클릭
3. "Deploy from GitHub repo" 선택
4. GitHub 저장소 선택: `Keep-K/Ratatuai_airdrop_bot`
5. Root Directory를 `airdrop-bot`으로 설정
6. 환경 변수 설정 (Variables 탭):
   - BOT_TOKEN
   - BOT_USERNAME
   - RECIPE_AI_BASE_URL
   - RECIPE_AI_TIMEOUT
   - DB_PATH

## 방법 3: Railway CLI로 빠른 배포

```bash
cd airdrop-bot
railway login
railway init
railway link  # 기존 프로젝트에 연결하는 경우
railway up
```

## 유용한 Railway 명령어

```bash
# 로그 확인
railway logs

# 환경 변수 확인
railway variables

# 환경 변수 삭제
railway variables unset VARIABLE_NAME

# 프로젝트 상태 확인
railway status

# 서비스 재시작
railway restart

# 프로젝트 목록 확인
railway list
```

