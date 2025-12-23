# Railway GitHub 연동 설정 가이드

## Railway 웹 UI 설정

### 1. 프로젝트 생성 및 GitHub 연결
1. Railway 웹사이트 (https://railway.app) 접속
2. "New Project" 클릭
3. "Deploy from GitHub repo" 선택
4. GitHub 저장소 선택: `Keep-K/Ratatuai_airdrop_bot`
5. 저장소 연결 확인

### 2. 서비스 설정 (Settings 탭)

#### Root Directory 설정
- **Root Directory**: (비워두기 또는 `.`로 설정)
  - 루트에 Dockerfile이 있으므로 루트를 기준으로 빌드합니다.

#### Build & Deploy 설정
Railway는 Dockerfile을 자동으로 감지하지만, 명시적으로 설정하려면:

**Build Command** (선택사항 - Dockerfile 사용 시 자동):
```
(비워두거나 자동 감지 사용)
```
또는 명시적으로:
```
docker build -t railway .
```

**Start Command** (선택사항 - Dockerfile의 CMD 사용):
```
(비워두거나 Dockerfile의 CMD 사용)
```
또는 명시적으로:
```
python bot.py
```

**Dockerfile Path** (자동 감지되지만 명시 가능):
```
Dockerfile
```

### 3. 환경 변수 설정 (Variables 탭)

다음 환경 변수들을 설정하세요:

```
BOT_TOKEN=your_telegram_bot_token_here
BOT_USERNAME=YourBotUsernameWithoutAt
RECIPE_AI_BASE_URL=https://recipe-ai-production.up.railway.app
RECIPE_AI_TIMEOUT=20
DB_PATH=/data/db.sqlite3
```

### 4. 볼륨 설정 (Volume 탭) - 선택사항

데이터베이스 영구 저장을 위해:
- **Mount Path**: `/data`
- 이렇게 설정하면 `DB_PATH=/data/db.sqlite3`로 설정한 데이터베이스가 영구 저장됩니다.

## 요약: 필수 설정 항목

### Settings 탭
- **Root Directory**: (비워두기 또는 `.`) - 루트의 Dockerfile 사용
- **Dockerfile Path**: `Dockerfile` (자동 감지됨)

### Variables 탭
- `BOT_TOKEN` ⚠️ 필수
- `BOT_USERNAME` ⚠️ 필수
- `RECIPE_AI_BASE_URL` (기본값 있음)
- `RECIPE_AI_TIMEOUT` (기본값 있음)
- `DB_PATH` (기본값 있음)

### Volume 탭 (선택사항)
- Mount Path: `/data` (DB 영구 저장용)

## 배포 후 확인

배포가 완료되면:
1. Deployments 탭에서 빌드 로그 확인
2. Logs 탭에서 봇 실행 로그 확인
3. Telegram에서 봇이 정상 작동하는지 테스트

