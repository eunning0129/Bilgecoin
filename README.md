# 빌지코인 — 여 운 포인트 관리 봇

## 시작
1. Python 3.11+ 환경에서 `pip install -r requirements.txt`.
2. `.env.example`의 키를 Railway Variables에 입력. 로컬에서는 환경변수로 지정하거나 python-dotenv 등을 이용해 로드하세요 (`bot.py` 자체는 .env 자동 로드 안 함).
3. Discord Developer Portal에서 Bot의 **Server Members Intent** 활성화, `bot` + `applications.commands` 스코프로 초대. 봇에게 메시지 전송, 임베드, 메시지 기록 조회, 슬래시 명령 사용 권한 부여.
4. Railway에 GitHub 프로젝트 배포, Start Command `python bot.py`. **Volume을 `/data`에 마운트**, `DATABASE_PATH=/data/bilgecoin.sqlite3`. 단일 replica로 실행.
5. `/로그설정`, `/상점설정` 실행. 상점 메시지는 수정 시 갱신되며 재시작 시에도 갱신 시도.
6. `INTEGRATION_SECRET`은 길고 무작위인 비밀값을 사용하고 절대 공개하지 마세요. Railway의 public HTTP 도메인을 연동 웹훅 URL로 사용. 포트는 `PORT` 변수.

## 별빛지기 연동
별빛지기에서 **경기가 실제로 최종 완료되어 확정되는 지점**에 아래 요청을 한 번 전송하세요. 경기 ID는 매 경기마다 고유해야 합니다. 같은 ID로 재전송해도 중복 지급되지 않습니다.

```python
import aiohttp
async def notify_bilgecoin_match(base_url, secret, guild_id, match_id, participant_ids):
    async with aiohttp.ClientSession() as session:
        async with session.post(base_url + '/events/match-completed',
            headers={'X-Integration-Secret': secret},
            json={'guild_id': guild_id, 'match_id': str(match_id), 'participant_ids': participant_ids},
            timeout=aiohttp.ClientTimeout(total=15)) as resp:
            resp.raise_for_status()
            return await resp.json()
```

`participant_ids`는 Discord 유저 ID 10개 리스트. 별빛지기의 코드가 없으므로 실제 완료 콜백 위치에 삽입하는 작업은 아직 필요합니다.

## 알룬 연동
초대자 보상은 **300P**입니다. 동일한 신규 유저의 졸업 보상은 한 번만 지급됩니다.
알룬에서 **내전 3경기 완료 확인 + 뉴비 역할 제거가 성공한 이후** 다음 요청을 보내세요. 초대자 ID는 신청서의 멘션을 검증해 추출한 Discord ID로 전달하세요. 초대자 정보가 빌지코인 `/초대자등록`에 이미 등록되어 있다면 `inviter_id`는 생략할 수 있습니다.

```python
import aiohttp
async def notify_bilgecoin_graduation(base_url, secret, guild_id, invitee_id, inviter_id):
    payload={'guild_id': guild_id, 'invitee_id': invitee_id, 'inviter_id': inviter_id,
             'completed_matches': 3, 'newbie_role_removed': True}
    async with aiohttp.ClientSession() as session:
        async with session.post(base_url + '/events/newbie-graduated',
            headers={'X-Integration-Secret': secret}, json=payload,
            timeout=aiohttp.ClientTimeout(total=15)) as resp:
            resp.raise_for_status()
            return await resp.json()
```

알룬은 **기존 서버 유저인지**, 초대 멘션이 실제 유효한지, 신규 유저가 뉴비 역할을 제거했는지를 직접 검증한 후 이벤트를 보내야 합니다. 빌지코인 API는 공유 비밀키를 가진 발신자를 신뢰하는 방식이며, 자체적으로 신청서나 역할 제거를 확인하지 않습니다.

## 기존 상품 예시
`/상품추가`로 다음을 등록하세요.
- 내전 취소권 1000P — 내전 1회를 취소할 수 있는 이용권
- 지인 내전 개최권 3000P — 지인과 함께하는 내전 개최 이용권
- 전적 초기화권 5000P — 내전 전적 초기화 이용권
- 닉네임 꾸미기 10000P — 닉네임 꾸미기 이용권
- 전용방 (월결제, 30일) 10000P — 30일 이용 가능한 전용방

**주의:** 아이템 사용은 관리자 수동 처리입니다. 실제 내전 취소, 전적 초기화, 닉네임 꾸미기, 전용방 생성·만료는 해당 운영 봇/관리자와 추가 연동해야 합니다. `/부스트지급`도 수동이며, `/후원지급`은 1원=1P. 상품 구매는 잔액 차감과 인벤토리 지급을 DB 트랜잭션으로 처리합니다. 구매/지급 로그 전송에 실패하면 DB 기록은 남고, 디스코드 로그 전송은 자동 재시도되지 않습니다. 중요 감사 기록은 DB 백업도 병행하세요.

## 제한/운영 주의
- 관리자에게 `관리자`, `서버 관리` 권한 또는 `ADMIN_ROLE_IDS`에 지정한 역할이 있어야 합니다.
- `/포인트수정`은 수정 직전 잔액을 기준으로 변동액을 계산합니다. 동시에 다른 지급이 발생할 수 있는 대규모 운영에서는 원자적 SET으로 개선 권장.
- 상품 삭제는 판매 중지로 처리해 구매/인벤토리 기록을 유지합니다.
- `/구매기록`, `/보유현황`은 출력 길이 제한으로 최근/일부만 보여줍니다.
- 수동 `/내전완료`는 쉼표로 구분한 참가자 10명 ID가 필요합니다.
- 내전 지급과 초대 지급은 고유 이벤트 키로 중복 방지합니다.
- 포인트 변동 로그는 기록하지만, 아이템 지급/회수/사용은 현재 별도 포인트 로그가 없습니다.
- `DATABASE_PATH`를 영속 볼륨에 두고 주기적으로 백업하세요.
