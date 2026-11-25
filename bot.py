import os, json, asyncio, logging, hmac
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import time
import discord
from discord import app_commands
from discord.ext import commands, tasks
import aiosqlite
from aiohttp import web

logging.basicConfig(level=logging.INFO)
TOKEN=os.getenv('DISCORD_TOKEN','')
GUILD_ID=int(os.getenv('GUILD_ID','0'))
LOG_ID=int(os.getenv('LOG_CHANNEL_ID','0'))
SHOP_ID=int(os.getenv('SHOP_CHANNEL_ID','0'))
NEWBIE_ROLE_ID=int(os.getenv('NEWBIE_ROLE_ID','0'))
ADMIN_IDS={int(x) for x in os.getenv('ADMIN_ROLE_IDS','').split(',') if x.strip().isdigit()}
DB_PATH=os.getenv('DATABASE_PATH','./bilgecoin.sqlite3')
SECRET=os.getenv('INTEGRATION_SECRET','')
PORT=int(os.getenv('PORT','8080'))
BOOST_REWARD=int(os.getenv('BOOST_REWARD','0'))
intents=discord.Intents.default(); intents.members=True; intents.voice_states=True
bot=commands.Bot(command_prefix='!',intents=intents)
db=None
lock=asyncio.Lock()

async def setup_db():
 global db
 os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)),exist_ok=True)
 db=await aiosqlite.connect(DB_PATH)
 await db.execute('PRAGMA journal_mode=WAL')
 await db.executescript('''
 CREATE TABLE IF NOT EXISTS balances(guild INTEGER,user INTEGER,points INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(guild,user));
 CREATE TABLE IF NOT EXISTS transactions(id INTEGER PRIMARY KEY AUTOINCREMENT,guild INTEGER,user INTEGER,delta INTEGER,balance INTEGER,reason TEXT,actor INTEGER,event_key TEXT UNIQUE,created TEXT DEFAULT CURRENT_TIMESTAMP);
 CREATE TABLE IF NOT EXISTS products(id INTEGER PRIMARY KEY AUTOINCREMENT,guild INTEGER,name TEXT,price INTEGER,description TEXT,stock INTEGER,active INTEGER DEFAULT 1);
 CREATE TABLE IF NOT EXISTS inventory(guild INTEGER,user INTEGER,product INTEGER,qty INTEGER,PRIMARY KEY(guild,user,product));
 CREATE TABLE IF NOT EXISTS purchases(id INTEGER PRIMARY KEY AUTOINCREMENT,guild INTEGER,user INTEGER,product INTEGER,name TEXT,price INTEGER,created TEXT DEFAULT CURRENT_TIMESTAMP);
 CREATE TABLE IF NOT EXISTS settings(guild INTEGER,key TEXT,value TEXT,PRIMARY KEY(guild,key));
 CREATE TABLE IF NOT EXISTS point_features(id INTEGER PRIMARY KEY AUTOINCREMENT,guild INTEGER NOT NULL,name TEXT NOT NULL,points INTEGER NOT NULL,description TEXT,active INTEGER NOT NULL DEFAULT 1);
 CREATE TABLE IF NOT EXISTS feature_requests(id INTEGER PRIMARY KEY AUTOINCREMENT,guild INTEGER NOT NULL,user INTEGER NOT NULL,feature INTEGER NOT NULL,status TEXT NOT NULL DEFAULT 'pending',note TEXT,reviewer INTEGER,created TEXT DEFAULT CURRENT_TIMESTAMP,reviewed TEXT);
 CREATE TABLE IF NOT EXISTS invitations(guild INTEGER,invitee INTEGER,inviter INTEGER,PRIMARY KEY(guild,invitee));
 CREATE TABLE IF NOT EXISTS events(event_key TEXT PRIMARY KEY,created TEXT DEFAULT CURRENT_TIMESTAMP);
 CREATE TABLE IF NOT EXISTS match_announcements(guild INTEGER NOT NULL,match_id TEXT NOT NULL,channel_id INTEGER NOT NULL,participant_ids TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,last_error TEXT,created TEXT DEFAULT CURRENT_TIMESTAMP,PRIMARY KEY(guild,match_id));
 ''')
 await db.commit()
 await economy_setup()

async def setting(guild,key,default=None):
 async with db.execute('SELECT value FROM settings WHERE guild=? AND key=?',(guild,key)) as c: row=await c.fetchone()
 return row[0] if row else default
async def set_setting(guild,key,value):
 await db.execute('INSERT INTO settings VALUES(?,?,?) ON CONFLICT(guild,key) DO UPDATE SET value=excluded.value',(guild,key,str(value)));await db.commit()
async def balance(guild,user):
 async with db.execute('SELECT points FROM balances WHERE guild=? AND user=?',(guild,user)) as c: r=await c.fetchone()
 return r[0] if r else 0
async def post_log(guild,user,delta,total,reason,actor):
 channel_id=int(await setting(guild,'log_channel',LOG_ID) or 0)
 if not channel_id:return
 ch=bot.get_channel(channel_id)
 if ch is None:
  try:ch=await bot.fetch_channel(channel_id)
  except Exception: return
 em=discord.Embed(title='🪙 빌지코인 포인트 기록',color=discord.Color.green() if delta>=0 else discord.Color.red(),timestamp=datetime.now(timezone.utc))
 em.add_field(name='대상',value=f'<@{user}>',inline=True);em.add_field(name='변동',value=f'{delta:+,} P',inline=True)
 em.add_field(name='잔액',value=f'{total:,} P',inline=True);em.add_field(name='사유',value=reason[:1000],inline=False)
 em.set_footer(text=f'처리자: {actor or "자동 시스템"}')
 try:await ch.send(embed=em,allowed_mentions=discord.AllowedMentions.none())
 except Exception:logging.exception('log send failed')

async def change(guild,user,delta,reason,actor=0,event_key=None,allow_negative=False):
 async with lock:
  try:
   await db.execute('BEGIN IMMEDIATE')
   if event_key:
    async with db.execute('SELECT 1 FROM transactions WHERE event_key=?',(event_key,)) as c:
     if await c.fetchone():await db.rollback();return None
   async with db.execute('SELECT points FROM balances WHERE guild=? AND user=?',(guild,user)) as c:r=await c.fetchone()
   old=r[0] if r else 0;new=old+delta
   if new<0 and not allow_negative:await db.rollback();raise ValueError('포인트가 부족합니다.')
   await db.execute('INSERT INTO balances VALUES(?,?,?) ON CONFLICT(guild,user) DO UPDATE SET points=excluded.points',(guild,user,new))
   await db.execute('INSERT INTO transactions(guild,user,delta,balance,reason,actor,event_key) VALUES(?,?,?,?,?,?,?)',(guild,user,delta,new,reason,actor,event_key))
   await db.commit()
  except Exception:
   await db.rollback();raise
 await post_log(guild,user,delta,new,reason,actor)
 return new

def admin(inter:discord.Interaction):
 if not isinstance(inter.user,discord.Member):return False
 return inter.user.id==inter.guild.owner_id or inter.user.guild_permissions.administrator or inter.user.guild_permissions.manage_guild or bool(ADMIN_IDS.intersection(r.id for r in inter.user.roles))
async def require_admin(inter):
 if admin(inter):return True
 await inter.response.send_message('⛔ 관리자 전용 명령어입니다.',ephemeral=True);return False
async def reply(inter,msg):
 if inter.response.is_done():await inter.followup.send(msg,ephemeral=True)
 else:await inter.response.send_message(msg,ephemeral=True)

@bot.tree.command(name='포인트',description='포인트 및 보유 아이템 조회')
async def points(inter:discord.Interaction,유저:discord.Member|None=None):
 target=유저 or inter.user
 if target.id!=inter.user.id and not admin(inter):return await reply(inter,'타인의 포인트는 관리자만 조회할 수 있습니다.')
 p=await balance(inter.guild_id,target.id)
 async with db.execute('SELECT p.name,i.qty FROM inventory i JOIN products p ON p.id=i.product WHERE i.guild=? AND i.user=? AND i.qty>0',(inter.guild_id,target.id)) as c:rows=await c.fetchall()
 em=discord.Embed(title=f'🪙 {target.display_name}의 보물상자',description=f'**잔액: {p:,} P**',color=discord.Color.gold())
 em.add_field(name='보유 아이템',value='\n'.join(f'{n} × {q}' for n,q in rows)[:1000] or '없음',inline=False)
 await inter.response.send_message(embed=em,ephemeral=True)

@bot.tree.command(name='포인트지급',description='[관리자] 포인트 지급')
async def give(inter:discord.Interaction,유저:discord.Member,포인트:app_commands.Range[int,1,100000000],사유:str):
 if not await require_admin(inter):return
 new=await change(inter.guild_id,유저.id,포인트,사유,inter.user.id)
 await reply(inter,f'✅ {유저.mention}에게 {포인트:,}P 지급 (잔액 {new:,}P)')
@bot.tree.command(name='포인트차감',description='[관리자] 포인트 차감')
async def deduct(inter:discord.Interaction,유저:discord.Member,포인트:app_commands.Range[int,1,100000000],사유:str):
 if not await require_admin(inter):return
 try:new=await change(inter.guild_id,유저.id,-포인트,사유,inter.user.id)
 except ValueError as e:return await reply(inter,str(e))
 await reply(inter,f'✅ {유저.mention}에게서 {포인트:,}P 차감 (잔액 {new:,}P)')
@bot.tree.command(name='포인트수정',description='[관리자] 잔액을 지정 값으로 변경')
async def set_points(inter:discord.Interaction,유저:discord.Member,포인트:app_commands.Range[int,0,100000000],사유:str):
 if not await require_admin(inter):return
 async with lock:
  current=await balance(inter.guild_id,유저.id)
  # lock must not be nested; calculate difference and apply below
 new=await change(inter.guild_id,유저.id,포인트-current,'잔액 수정: '+사유,inter.user.id)
 await reply(inter,f'잔액을 {new:,}P로 변경했습니다.')
@bot.tree.command(name='포인트내역',description='최근 포인트 변동 내역')
async def history(inter:discord.Interaction,유저:discord.Member|None=None):
 target=유저 or inter.user
 if target.id!=inter.user.id and not admin(inter):return await reply(inter,'관리자만 타인의 기록을 볼 수 있습니다.')
 async with db.execute('SELECT delta,balance,reason,created FROM transactions WHERE guild=? AND user=? ORDER BY id DESC LIMIT 10',(inter.guild_id,target.id)) as c:rows=await c.fetchall()
 msg='\n'.join(f'`{dt}` **{d:+,}P** → {b:,}P · {r[:65]}' for d,b,r,dt in rows) or '기록 없음'
 await reply(inter,msg[:1900])

async def shop_embed(guild):
 async with db.execute('SELECT id,name,price,description,stock FROM products WHERE guild=? AND active=1 ORDER BY price,id',(guild,)) as c:rows=await c.fetchall()
 em=discord.Embed(title='🏴‍☠️ 빌지코인 보물상점',description='관리자가 등록한 상점 상품 안내입니다.\n포인트 기능 사용은 `/포인트상품`에서 별도로 요청하세요.',color=discord.Color.gold())
 if not rows:em.add_field(name='상품 준비 중',value='등록된 상품이 없습니다.',inline=False)
 for pid,name,price,desc,stock in rows[:25]:
  em.add_field(name=f'#{pid} · {name} — {price:,}P',value=(desc or '설명 없음')[:850]+f'\n재고: {stock if stock>=0 else "무제한"}',inline=False)
 if len(rows)>25:em.set_footer(text=f'전체 {len(rows)}개 중 25개 표시')
 return em
async def refresh_shop(guild):
 cid=int(await setting(guild,'shop_channel',SHOP_ID) or 0)
 if not cid:return False
 try:
  ch=bot.get_channel(cid) or await bot.fetch_channel(cid)
  em=await shop_embed(guild)
  mid=await setting(guild,'shop_message')
  if mid:
   try:
    msg=await ch.fetch_message(int(mid));await msg.edit(embed=em);return True
   except discord.NotFound:pass
  msg=await ch.send(embed=em);await set_setting(guild,'shop_message',msg.id);return True
 except Exception:logging.exception('shop refresh failed');return False

@bot.tree.command(name='상점',description='포인트 상점 목록 조회')
async def shop(inter:discord.Interaction):await inter.response.send_message(embed=await shop_embed(inter.guild_id),ephemeral=True)
@bot.tree.command(name='상점설정',description='[관리자] 상점 임베드 채널 지정')
async def shop_set(inter:discord.Interaction,채널:discord.TextChannel):
 if not await require_admin(inter):return
 await set_setting(inter.guild_id,'shop_channel',채널.id)
 await reply(inter,'상점 채널 지정 완료. '+('임베드 게시 성공' if await refresh_shop(inter.guild_id) else '게시 실패: 봇 채널 권한을 확인하세요.'))
@bot.tree.command(name='로그설정',description='[관리자] 포인트 로그 채널 지정')
async def log_set(inter:discord.Interaction,채널:discord.TextChannel):
 if not await require_admin(inter):return
 await set_setting(inter.guild_id,'log_channel',채널.id);await reply(inter,'로그 채널을 지정했습니다.')
@bot.tree.command(name='상품추가',description='[관리자] 상점 상품 추가')
async def add_product(inter:discord.Interaction,이름:str,가격:app_commands.Range[int,0,100000000],설명:str,재고:int=-1):
 if not await require_admin(inter):return
 if 재고 < -1:return await reply(inter,'재고는 -1(무제한) 이상이어야 합니다.')
 cur=await db.execute('INSERT INTO products(guild,name,price,description,stock) VALUES(?,?,?,?,?)',(inter.guild_id,이름,가격,설명,재고));await db.commit()
 ok=await refresh_shop(inter.guild_id);await reply(inter,f'상품 #{cur.lastrowid} 등록 완료 · 상점 갱신: {ok}')
@bot.tree.command(name='상품수정',description='[관리자] 상품 이름·가격·설명·재고 수정')
async def edit_product(inter:discord.Interaction,상품번호:int,이름:str,가격:app_commands.Range[int,0,100000000],설명:str,재고:int=-1):
 if not await require_admin(inter):return
 if 재고 < -1:return await reply(inter,'재고는 -1 이상이어야 합니다.')
 cur=await db.execute('UPDATE products SET name=?,price=?,description=?,stock=? WHERE guild=? AND id=? AND active=1',(이름,가격,설명,재고,inter.guild_id,상품번호));await db.commit()
 if not cur.rowcount:return await reply(inter,'상품을 찾을 수 없습니다.')
 await refresh_shop(inter.guild_id);await reply(inter,'상품 수정 완료')
@bot.tree.command(name='상품삭제',description='[관리자] 상품 판매 중지')
async def remove_product(inter:discord.Interaction,상품번호:int):
 if not await require_admin(inter):return
 cur=await db.execute('UPDATE products SET active=0 WHERE guild=? AND id=?',(inter.guild_id,상품번호));await db.commit();await refresh_shop(inter.guild_id)
 await reply(inter,'판매 중지 완료' if cur.rowcount else '상품 없음')
@bot.tree.command(name='상점새로고침',description='[관리자] 상점 임베드 수동 갱신')
async def refresh_cmd(inter:discord.Interaction):
 if not await require_admin(inter):return
 await reply(inter,'상점 임베드 갱신 '+('성공' if await refresh_shop(inter.guild_id) else '실패'))

@bot.tree.command(name='구매',description='상점 상품 안내 (관리자 지급 방식)')
async def buy(inter:discord.Interaction,상품번호:int):
 async with db.execute('SELECT name FROM products WHERE guild=? AND id=? AND active=1',(inter.guild_id,상품번호)) as c:r=await c.fetchone()
 if not r:return await reply(inter,'상점 상품을 찾을 수 없습니다.')
 await reply(inter,f'🛒 **{r[0]}**은(는) 상점 안내 상품입니다. 관리진에게 지급을 문의해 주세요.\n포인트로 특정 기능을 이용하려면 `/포인트상품` → `/포인트상품요청`을 사용하세요.')

@bot.tree.command(name='구매기록',description='[관리자] 유저 구매 기록 조회')
async def purchase_history(inter:discord.Interaction,유저:discord.Member):
 if not await require_admin(inter):return
 async with db.execute('SELECT name,price,created FROM purchases WHERE guild=? AND user=? ORDER BY id DESC LIMIT 15',(inter.guild_id,유저.id)) as c:rows=await c.fetchall()
 await reply(inter,'\n'.join(f'{t} · {n} ({p:,}P)' for n,p,t in rows)[:1900] or '구매 기록 없음')
@bot.tree.command(name='보유현황',description='[관리자] 전체 보유 아이템 현황')
async def inventory_all(inter:discord.Interaction):
 if not await require_admin(inter):return
 async with db.execute('SELECT i.user,p.name,i.qty FROM inventory i JOIN products p ON i.product=p.id WHERE i.guild=? AND i.qty>0 ORDER BY i.user LIMIT 40',(inter.guild_id,)) as c:rows=await c.fetchall()
 await reply(inter,'\n'.join(f'<@{u}> · {n} ×{q}' for u,n,q in rows)[:1900] or '보유 아이템 없음')
@bot.tree.command(name='아이템지급',description='[관리자] 상품을 유저 인벤토리에 지급')
async def item_give(inter:discord.Interaction,유저:discord.Member,상품번호:int,수량:app_commands.Range[int,1,100]=1):
 if not await require_admin(inter):return
 async with db.execute('SELECT name FROM products WHERE guild=? AND id=?',(inter.guild_id,상품번호)) as c:r=await c.fetchone()
 if not r:return await reply(inter,'상품 없음')
 await db.execute('INSERT INTO inventory VALUES(?,?,?,?) ON CONFLICT(guild,user,product) DO UPDATE SET qty=qty+excluded.qty',(inter.guild_id,유저.id,상품번호,수량));await db.commit()
 await reply(inter,f'{유저.mention}에게 {r[0]} ×{수량} 지급 완료')
@bot.tree.command(name='아이템회수',description='[관리자] 아이템 회수 (선택적으로 포인트 환불)')
async def item_take(inter:discord.Interaction,유저:discord.Member,상품번호:int,환불:bool=False):
 if not await require_admin(inter):return
 async with lock:
  try:
   await db.execute('BEGIN IMMEDIATE')
   async with db.execute('SELECT i.qty,p.name,p.price FROM inventory i JOIN products p ON i.product=p.id WHERE i.guild=? AND i.user=? AND i.product=?',(inter.guild_id,유저.id,상품번호)) as c:r=await c.fetchone()
   if not r or r[0]<1:raise ValueError('해당 아이템을 보유하고 있지 않습니다.')
   qty,name,price=r
   await db.execute('UPDATE inventory SET qty=qty-1 WHERE guild=? AND user=? AND product=?',(inter.guild_id,유저.id,상품번호))
   new=None
   if 환불:
    async with db.execute('SELECT points FROM balances WHERE guild=? AND user=?',(inter.guild_id,유저.id)) as c:b=await c.fetchone()
    new=(b[0] if b else 0)+price
    await db.execute('INSERT INTO balances VALUES(?,?,?) ON CONFLICT(guild,user) DO UPDATE SET points=excluded.points',(inter.guild_id,유저.id,new))
    await db.execute('INSERT INTO transactions(guild,user,delta,balance,reason,actor) VALUES(?,?,?,?,?,?)',(inter.guild_id,유저.id,price,new,f'아이템 회수 환불: {name}',inter.user.id))
   await db.commit()
  except ValueError as e:await db.rollback();return await reply(inter,str(e))
  except Exception:await db.rollback();raise
 if 환불:await post_log(inter.guild_id,유저.id,price,new,f'아이템 회수 환불: {name}',inter.user.id)
 await reply(inter,f'{name} 1개 회수 완료'+(f' · {price:,}P 환불' if 환불 else ''))
@bot.tree.command(name='아이템사용',description='[관리자] 유저 아이템 1개 사용 처리')
async def item_use(inter:discord.Interaction,유저:discord.Member,상품번호:int):
 if not await require_admin(inter):return
 async with lock:
  cur=await db.execute('UPDATE inventory SET qty=qty-1 WHERE guild=? AND user=? AND product=? AND qty>0',(inter.guild_id,유저.id,상품번호));await db.commit()
 await reply(inter,'아이템 사용 처리 완료' if cur.rowcount else '보유 아이템 없음')

# 포인트상품은 상점(products)과 별도 테이블에서 관리하며 관리자 승인 전에는 포인트가 움직이지 않습니다.
@bot.tree.command(name='포인트상품',description='포인트로 이용 가능한 기능 목록 조회')
async def point_features(inter:discord.Interaction):
 async with db.execute('SELECT id,name,points,description FROM point_features WHERE guild=? AND active=1 ORDER BY id',(inter.guild_id,)) as c:rows=await c.fetchall()
 em=discord.Embed(title='🪙 빌지코인 포인트상품',description='기능 사용은 `/포인트상품요청`으로 신청하고 관리자 승인 후 정산됩니다.\n양수는 지급, 음수는 차감입니다.',color=discord.Color.gold())
 for fid,name,points,description in rows[:25]:
  em.add_field(name=f'#{fid} {name} · {points:+,}P',value=(description or '설명 없음')[:900],inline=False)
 if not rows:em.description='등록된 포인트상품이 없습니다.'
 await inter.response.send_message(embed=em,ephemeral=True)

@bot.tree.command(name='포인트상품추가',description='[관리자] 기능과 정산 포인트 등록 (음수=차감)')
async def feature_add(inter:discord.Interaction,이름:str,포인트:app_commands.Range[int,-100000000,100000000],설명:str):
 if not await require_admin(inter):return
 cur=await db.execute('INSERT INTO point_features(guild,name,points,description) VALUES(?,?,?,?)',(inter.guild_id,이름,포인트,설명));await db.commit()
 await reply(inter,f'포인트상품 #{cur.lastrowid} 등록 완료 ({포인트:+,}P)')

@bot.tree.command(name='포인트상품수정',description='[관리자] 포인트상품 정보 변경')
async def feature_edit(inter:discord.Interaction,상품번호:int,이름:str,포인트:app_commands.Range[int,-100000000,100000000],설명:str):
 if not await require_admin(inter):return
 cur=await db.execute('UPDATE point_features SET name=?,points=?,description=? WHERE guild=? AND id=? AND active=1',(이름,포인트,설명,inter.guild_id,상품번호));await db.commit()
 await reply(inter,'수정 완료' if cur.rowcount else '상품을 찾지 못했습니다.')

@bot.tree.command(name='포인트상품삭제',description='[관리자] 포인트상품 비활성화')
async def feature_delete(inter:discord.Interaction,상품번호:int):
 if not await require_admin(inter):return
 cur=await db.execute('UPDATE point_features SET active=0 WHERE guild=? AND id=?',(inter.guild_id,상품번호));await db.commit()
 await reply(inter,'삭제 완료' if cur.rowcount else '상품을 찾지 못했습니다.')

@bot.tree.command(name='포인트상품요청',description='기능 사용 요청 (관리자 확인 후 정산)')
async def feature_request(inter:discord.Interaction,상품번호:int,요청내용:str=''):
 async with db.execute('SELECT name,points FROM point_features WHERE guild=? AND id=? AND active=1',(inter.guild_id,상품번호)) as c:r=await c.fetchone()
 if not r:return await reply(inter,'사용 가능한 포인트상품이 아닙니다.')
 cur=await db.execute('INSERT INTO feature_requests(guild,user,feature,note) VALUES(?,?,?,?)',(inter.guild_id,inter.user.id,상품번호,요청내용[:500]));await db.commit()
 await reply(inter,f'신청 #{cur.lastrowid} 접수 완료: **{r[0]}** ({r[1]:+,}P)\n관리자가 확인하기 전까지 포인트는 변동되지 않습니다.')
 # 관리자가 요청을 확인할 수 있도록 로그 채널에 별도 알림
 cid=int(await setting(inter.guild_id,'log_channel',LOG_ID) or 0)
 if cid:
  try:
   ch=bot.get_channel(cid) or await bot.fetch_channel(cid)
   await ch.send(f'📩 **포인트상품 신청 #{cur.lastrowid}** · <@{inter.user.id}> · {r[0]} ({r[1]:+,}P)\n관리자: `/포인트상품승인` 또는 `/포인트상품거절`',allowed_mentions=discord.AllowedMentions.none())
  except Exception:logging.exception('feature request notification failed')

@bot.tree.command(name='포인트상품요청목록',description='[관리자] 처리 대기 중인 요청 조회')
async def feature_pending(inter:discord.Interaction):
 if not await require_admin(inter):return
 async with db.execute('SELECT r.id,r.user,f.name,f.points,r.note FROM feature_requests r JOIN point_features f ON f.id=r.feature WHERE r.guild=? AND r.status="pending" ORDER BY r.id LIMIT 25',(inter.guild_id,)) as c:rows=await c.fetchall()
 await reply(inter,('\n'.join(f'#{rid} · <@{uid}> · {name} ({pts:+,}P) · {(note or "-")[:45]}' for rid,uid,name,pts,note in rows) or '대기 중인 요청 없음')[:1900])

@bot.tree.command(name='포인트상품승인',description='[관리자] 요청 승인 및 포인트 정산')
async def feature_approve(inter:discord.Interaction,요청번호:int):
 if not await require_admin(inter):return
 await inter.response.defer(ephemeral=True)
 async with lock:
  try:
   await db.execute('BEGIN IMMEDIATE')
   async with db.execute('SELECT r.user,r.status,f.name,f.points FROM feature_requests r JOIN point_features f ON f.id=r.feature WHERE r.guild=? AND r.id=?',(inter.guild_id,요청번호)) as c:r=await c.fetchone()
   if not r or r[1]!='pending':raise ValueError('처리 대기 중인 요청이 아닙니다.')
   uid,_,name,delta=r
   async with db.execute('SELECT points FROM balances WHERE guild=? AND user=?',(inter.guild_id,uid)) as c:b=await c.fetchone()
   new=(b[0] if b else 0)+delta
   if new<0:raise ValueError('요청자의 포인트가 부족합니다.')
   await db.execute('INSERT INTO balances VALUES(?,?,?) ON CONFLICT(guild,user) DO UPDATE SET points=excluded.points',(inter.guild_id,uid,new))
   await db.execute('INSERT INTO transactions(guild,user,delta,balance,reason,actor,event_key) VALUES(?,?,?,?,?,?,?)',(inter.guild_id,uid,delta,new,f'포인트상품 승인: {name} · 요청 #{요청번호}',inter.user.id,f'feature-request:{inter.guild_id}:{요청번호}'))
   await db.execute('UPDATE feature_requests SET status="approved",reviewer=?,reviewed=CURRENT_TIMESTAMP WHERE guild=? AND id=?',(inter.user.id,inter.guild_id,요청번호))
   await db.commit()
  except ValueError as e:
   await db.rollback();return await inter.followup.send(str(e),ephemeral=True)
  except Exception:
   await db.rollback();raise
 await post_log(inter.guild_id,uid,delta,new,f'포인트상품 승인: {name} · 요청 #{요청번호}',inter.user.id)
 await inter.followup.send(f'승인 완료: <@{uid}> · {name} · {delta:+,}P · 잔액 {new:,}P\n실제 기능 제공은 관리자가 별도로 처리해 주세요.',ephemeral=True)

@bot.tree.command(name='포인트상품거절',description='[관리자] 요청 거절 (포인트 변동 없음)')
async def feature_reject(inter:discord.Interaction,요청번호:int,사유:str):
 if not await require_admin(inter):return
 async with lock:
  cur=await db.execute('UPDATE feature_requests SET status="rejected",reviewer=?,reviewed=CURRENT_TIMESTAMP,note=COALESCE(note,"") || ? WHERE guild=? AND id=? AND status="pending"',(inter.user.id,' | 거절 사유: '+사유[:200],inter.guild_id,요청번호));await db.commit()
 await reply(inter,'요청 거절 완료 (포인트 변동 없음)' if cur.rowcount else '대기 중인 요청을 찾지 못했습니다.')

@bot.tree.command(name='후원지급',description='[관리자] 후원 금액에 따라 포인트 지급 (1원=1P)')
async def donation(inter:discord.Interaction,유저:discord.Member,후원금액:app_commands.Range[int,1,100000000]):
 if not await require_admin(inter):return
 p=await change(inter.guild_id,유저.id,후원금액,f'후원 보상: {후원금액:,}원',inter.user.id);await reply(inter,f'후원 포인트 지급 완료 · 잔액 {p:,}P')
@bot.tree.command(name='부스트지급',description='[관리자] 서버 부스트 포인트 지급')
async def boost(inter:discord.Interaction,유저:discord.Member,포인트:app_commands.Range[int,1,100000000]):
 if not await require_admin(inter):return
 p=await change(inter.guild_id,유저.id,포인트,'서버 부스트 보상',inter.user.id);await reply(inter,f'부스트 보상 지급 완료 · 잔액 {p:,}P')
@bot.tree.command(name='초대자등록',description='[관리자] 신규 유저의 초대자 지정')
async def invite_register(inter:discord.Interaction,신규유저:discord.Member,초대자:discord.Member):
 if not await require_admin(inter):return
 if 신규유저.id==초대자.id:return await reply(inter,'자기 자신을 초대할 수 없습니다.')
 await db.execute('INSERT INTO invitations VALUES(?,?,?) ON CONFLICT(guild,invitee) DO UPDATE SET inviter=excluded.inviter',(inter.guild_id,신규유저.id,초대자.id));await db.commit()
 await reply(inter,f'초대자 등록 완료: {신규유저.mention} → {초대자.mention}')
@bot.tree.command(name='내전완료',description='[관리자] 내전 자동 지급 실패 시 수동 처리 (경기 ID 중복 방지)')
async def match_manual(inter:discord.Interaction,경기아이디:str,참가자아이디목록:str,내전유형:str="mobashi"):
 if not await require_admin(inter):return
 try:users=list(dict.fromkeys(int(x.strip()) for x in 참가자아이디목록.split(',') if x.strip()))
 except ValueError:return await reply(inter,'참가자 ID를 쉼표로 구분해 입력하세요.')
 if 내전유형 not in MATCH_REWARDS or len(users)!=MATCH_REWARDS[내전유형][1]:return await reply(inter,'내전 유형 또는 참가자 수를 확인하세요.')
 await inter.response.defer(ephemeral=True)
 n=await reward_match(inter.guild_id,경기아이디,users,내전유형)
 await inter.followup.send(f'경기 보상 처리 완료: {n}명 신규 지급',ephemeral=True)

async def reward_match(guild,match_id,users,match_type="legacy"):
 amount, size = MATCH_REWARDS[match_type] if match_type in MATCH_REWARDS else (0,0)
 users = [int(u) for u in users]
 if not match_id or len(users)!=size or len(set(users))!=size or any(u<=0 for u in users):raise ValueError('고유 경기 ID와 유형에 맞는 중복 없는 참가자가 필요합니다.')
 count=0
 for uid in users:
  result=await change(guild,int(uid),amount,f'내전 완료 보상 · 경기 {match_id}',0,f'match:{guild}:{match_id}:{uid}')
  if result is not None:count+=1
 return count
async def reward_invite(guild,invitee,inviter=None):
 if inviter is None:
  async with db.execute('SELECT inviter FROM invitations WHERE guild=? AND invitee=?',(guild,invitee)) as c:r=await c.fetchone()
  if not r:return None  # 초대자 미등록: 졸업 정상 처리, 초대 보상 없음
  inviter=r[0]
 if invitee==inviter:raise ValueError('자기 자신은 초대자가 될 수 없습니다.')
 # Only award once per invitee, even if inviter is later changed.
 return await change(guild,inviter,300,f'뉴비 3경기 졸업 초대 보상 · 신규 유저 {invitee}',0,f'invite-graduation:{guild}:{invitee}')

# Bilgecoin itself posts the completion announcement, with durable retries.
announcement_lock=asyncio.Lock()
async def queue_match_announcement(guild,match_id,channel_id,users):
 if channel_id<=0:return
 async with announcement_lock:
  await db.execute("""INSERT INTO match_announcements(guild,match_id,channel_id,participant_ids)
    VALUES(?,?,?,?) ON CONFLICT(guild,match_id) DO NOTHING""",
    (guild,match_id,channel_id,json.dumps(users)))
  await db.commit()

async def send_match_announcement(guild,match_id):
 async with announcement_lock:
  async with db.execute('SELECT channel_id,participant_ids,status FROM match_announcements WHERE guild=? AND match_id=?',(guild,match_id)) as cur:
   row=await cur.fetchone()
  if not row or row[2]=='sent':return True
  channel_id,raw,_=row
  try:
   if not bot.is_ready():raise RuntimeError('Discord gateway is not ready')
   ch=bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
   if getattr(ch,'guild',None) is None or ch.guild.id!=guild:raise ValueError('channel does not belong to guild')
   users=json.loads(raw)
   async with db.execute('SELECT delta FROM transactions WHERE event_key=?',(f'match:{guild}:{match_id}:{users[0]}',)) as cur: reward=await cur.fetchone()
   amount=reward[0] if reward else 100
   mentions=' '.join(f'<@{uid}>' for uid in users)
   await ch.send('🏴‍☠️ **빌지코인 | 내전 참가 완료 보상**\n'
                 f'{mentions}\n\n'
                 f'🪙 **내전 참가 완료 보상 {amount:,}P가 지급 완료되었습니다!**\n'
                 '참여해 주셔서 감사합니다! 💰',
                 allowed_mentions=discord.AllowedMentions(users=True,roles=False,everyone=False))
   await db.execute("UPDATE match_announcements SET status='sent',last_error=NULL WHERE guild=? AND match_id=?",(guild,match_id))
   await db.commit()
   return True
  except Exception as exc:
   logging.exception('빌지코인 내전 완료 채널 알림 실패 match=%s',match_id)
   await db.execute('UPDATE match_announcements SET attempts=attempts+1,last_error=? WHERE guild=? AND match_id=?',(str(exc)[:400],guild,match_id))
   await db.commit()
   return False

@tasks.loop(seconds=60)
async def retry_match_announcements():
 if not bot.is_ready():return
 async with db.execute("SELECT guild,match_id FROM match_announcements WHERE status='pending' ORDER BY created LIMIT 20") as cur:
  rows=await cur.fetchall()
 for guild,match_id in rows:
  await send_match_announcement(guild,match_id)

@retry_match_announcements.before_loop
async def before_announcement_retry():
 await bot.wait_until_ready()

async def api_inviter(req):
 data=await req.json()
 guild=int(data.get('guild_id',0));invitee=int(data.get('invitee_id',0));inviter=int(data.get('inviter_id',0))
 if not guild or guild != GUILD_ID or not invitee or not inviter or invitee==inviter or data.get('inviter_joined_before_invitee') is not True:
  raise web.HTTPBadRequest(text='invalid invitation')
 async with lock:
  await db.execute('INSERT INTO invitations(guild,invitee,inviter) VALUES(?,?,?) ON CONFLICT(guild,invitee) DO NOTHING',(guild,invitee,inviter))
  await db.commit()
 return web.json_response({'ok':True})

async def api_match(req):
 data=await req.json()
 guild=int(data.get('guild_id',GUILD_ID));users=data.get('participant_ids',[])
 if not guild or guild != GUILD_ID or not isinstance(users,list):raise web.HTTPBadRequest(text='invalid guild or participants')
 try:n=await reward_match(guild,str(data['match_id']),users,data.get('match_type','legacy'))
 except (ValueError,KeyError,TypeError) as e:raise web.HTTPBadRequest(text=str(e))
 logging.info('내전 보상 처리 guild=%s match=%s newly_awarded=%s',guild,data['match_id'],n)
 channel_id=int(data.get('channel_id') or 0)
 if channel_id:
  await queue_match_announcement(guild,str(data['match_id']),channel_id,users)
  announced=await send_match_announcement(guild,str(data['match_id']))
 else:announced=False
 return web.json_response({'ok':True,'awarded':n,'already_awarded':len(users)-n,'announcement_sent':announced})
async def api_graduate(req):
 data=await req.json();guild=int(data.get('guild_id',GUILD_ID));invitee=int(data['invitee_id'])
 if not guild or guild != GUILD_ID:raise web.HTTPBadRequest(text='invalid guild')
 # Require the sender to assert completion AND role removal; this endpoint is not a Discord verification oracle.
 if data.get('completed_matches',0)<3 or data.get('newbie_role_removed') is not True:raise web.HTTPBadRequest(text='graduation not confirmed')
 try:result=await reward_invite(guild,invitee,None)
 except ValueError as e:raise web.HTTPBadRequest(text=str(e))
 return web.json_response({'ok':True,'awarded':result is not None})
@web.middleware
async def authenticate(req,handler):
 if not SECRET or not hmac.compare_digest(req.headers.get('X-Integration-Secret',''),SECRET):raise web.HTTPUnauthorized()
 return await handler(req)
async def start_api():
 app=web.Application(middlewares=[authenticate]);app.router.add_post('/events/inviter-registered',api_inviter);app.router.add_post('/events/match-completed',api_match);app.router.add_post('/events/newbie-graduated',api_graduate)
 runner=web.AppRunner(app);await runner.setup();site=web.TCPSite(runner,'0.0.0.0',PORT);await site.start()
 logging.info('Integration API listening on %s',PORT)

@bot.event
async def on_ready():
 logging.info('Logged in as %s',bot.user)
 if not retry_match_announcements.is_running():retry_match_announcements.start()
 if GUILD_ID:
  await refresh_shop(GUILD_ID)
 if not economy_tick.is_running():economy_tick.start()
 await publish_panels()

async def main():
 if not TOKEN:raise RuntimeError('DISCORD_TOKEN required')
 await setup_db()
 async def setup_hook():
  if GUILD_ID:
   guild=discord.Object(id=GUILD_ID)
   bot.tree.copy_global_to(guild=guild)
   await bot.tree.sync(guild=guild)
  else:await bot.tree.sync()
 bot.setup_hook=setup_hook
 await start_api()
 await bot.start(TOKEN)



# 여운 경제 시스템: 기존 테이블을 유지하고 신규 테이블만 추가.
KST=ZoneInfo('Asia/Seoul')
MATCH_REWARDS={'legacy':(100,10),'mobashi':(1000,10),'low':(1000,10),'high':(1000,10),'single':(300,10),'twenty':(1000,20)}
async def rows(sql,args=()):
 async with db.execute(sql,args) as c:return await c.fetchall()
async def economy_setup():
 await db.executescript('''
 CREATE TABLE IF NOT EXISTS attendance(guild INTEGER,user INTEGER,day TEXT,rank INTEGER,PRIMARY KEY(guild,user,day),UNIQUE(guild,day,rank));
 CREATE TABLE IF NOT EXISTS attendance_awards(guild INTEGER,month TEXT,body TEXT,posted INTEGER DEFAULT 0,PRIMARY KEY(guild,month));
 CREATE TABLE IF NOT EXISTS voice_total(guild INTEGER,user INTEGER,seconds INTEGER DEFAULT 0,remainder INTEGER DEFAULT 0,PRIMARY KEY(guild,user));
 CREATE TABLE IF NOT EXISTS voice_daily(guild INTEGER,user INTEGER,day TEXT,seconds INTEGER,PRIMARY KEY(guild,user,day));
 CREATE TABLE IF NOT EXISTS icons(guild INTEGER,role INTEGER,active INTEGER DEFAULT 1,PRIMARY KEY(guild,role));
 CREATE TABLE IF NOT EXISTS icon_owned(guild INTEGER,user INTEGER,role INTEGER,PRIMARY KEY(guild,user,role));
 ''')
 async with lock:
  try:
   await db.execute('BEGIN IMMEDIATE')
   if not await rows('SELECT 1 FROM settings WHERE guild=? AND key=?',(GUILD_ID,'economy_v1')):
    await db.execute('UPDATE products SET price=price*10 WHERE guild=?',(GUILD_ID,))
    await db.execute('UPDATE point_features SET points=points*10 WHERE guild=?',(GUILD_ID,))
    for name,price in [('내전취소권',10000),('지인 내전 개최권',30000),('전적 초기화권',50000),('전용방',100000)]:
     await db.execute('UPDATE point_features SET points=? WHERE guild=? AND replace(name," ","") LIKE ?',( -price,GUILD_ID,'%'+name.replace(' ','')+'%'))
    await db.execute('UPDATE point_features SET active=0 WHERE guild=? AND replace(name," ","") LIKE "%닉네임꾸미기%"',(GUILD_ID,))
    await db.execute('UPDATE products SET active=0 WHERE guild=? AND replace(name," ","") LIKE "%닉네임꾸미기%"',(GUILD_ID,))
    await db.execute('INSERT INTO settings VALUES(?,?,?)',(GUILD_ID,'economy_v1','1'))
   await db.commit()
  except Exception:await db.rollback();raise

async def credit_tx(g,u,delta,reason,key):
 old=await balance(g,u); new=old+delta
 if new<0:raise ValueError('포인트가 부족합니다.')
 await db.execute('INSERT INTO transactions(guild,user,delta,balance,reason,actor,event_key) VALUES(?,?,?,?,?,0,?)',(g,u,delta,new,reason,key))
 await db.execute('INSERT INTO balances VALUES(?,?,?) ON CONFLICT(guild,user) DO UPDATE SET points=excluded.points',(g,u,new))
 return new

@bot.tree.command(name='출석',description='매일 선착순 출석 체크')
async def attendance(inter:discord.Interaction):
 g,u=inter.guild_id,inter.user.id; day=datetime.now(KST).date().isoformat()
 async with lock:
  try:
   await db.execute('BEGIN IMMEDIATE')
   old=await rows('SELECT rank FROM attendance WHERE guild=? AND user=? AND day=?',(g,u,day))
   if old:await db.rollback();return await reply(inter,f'이미 출석했습니다. 오늘 {old[0][0]}등')
   rank=(await rows('SELECT COALESCE(MAX(rank),0)+1 FROM attendance WHERE guild=? AND day=?',(g,day)))[0][0]
   amount={1:3000,2:2000,3:1000}.get(rank,50)
   await db.execute('INSERT INTO attendance VALUES(?,?,?,?)',(g,u,day,rank))
   total=await credit_tx(g,u,amount,'일일 출석',f'attendance:{g}:{u}:{day}')
   await db.commit()
  except Exception:await db.rollback();raise
 await reply(inter,f'🎉 출석 완료! 오늘 {rank}등 · +{amount:,}P · 잔액 {total:,}P')
 await post_log(g,u,amount,total,'일일 출석',0)

@bot.tree.command(name='출석순위',description='이번 달 출석 순위')
async def attendance_rank(inter:discord.Interaction):
 month=datetime.now(KST).strftime('%Y-%m')
 data=await rows('SELECT user,COUNT(*) n FROM attendance WHERE guild=? AND day LIKE ? GROUP BY user ORDER BY n DESC,user LIMIT 25',(inter.guild_id,month+'%'))
 await reply(inter,rank_text(data))
def rank_text(data):
 last=None; rank=0; lines=[]
 for i,(u,n) in enumerate(data,1):
  if n!=last:rank=i;last=n
  lines.append(f'{rank}위 · <@{u}> · {n}일')
 return '\n'.join(lines)[:3900] or '출석 기록 없음'
@bot.tree.command(name='출석현황',description='나의 월간 출석 일수')
async def attendance_status(inter:discord.Interaction):
 n=(await rows('SELECT COUNT(*) FROM attendance WHERE guild=? AND user=? AND day LIKE ?',(inter.guild_id,inter.user.id,datetime.now(KST).strftime('%Y-%m')+'%')))[0][0]
 await reply(inter,f'이번 달 출석 {n}일')

async def monthly(g,now):
 if now.day<10:return
 month=(now.replace(day=1)-timedelta(days=1)).strftime('%Y-%m')
 async with lock:
  try:
   await db.execute('BEGIN IMMEDIATE')
   saved=await rows('SELECT body,posted FROM attendance_awards WHERE guild=? AND month=?',(g.id,month))
   if not saved:
    data=await rows('SELECT user,COUNT(*) n FROM attendance WHERE guild=? AND day LIKE ? GROUP BY user ORDER BY n DESC,user',(g.id,month+'%'))
    data=[(u,n) for u,n in data if g.get_member(u) is not None]
    for u,n in data:
     if n!=data[0][1]:break
     await credit_tx(g.id,u,5000,'월간 공동 출석왕',f'attendance-king:{g.id}:{month}:{u}')
    body='공동 1위 전원 5,000P 지급\n'+rank_text(data[:25]) if data else '출석 기록 없음'
    await db.execute('INSERT INTO attendance_awards VALUES(?,?,?,0)',(g.id,month,body));saved=[(body,0)]
   await db.commit()
  except Exception:await db.rollback();raise
 if saved[0][1]:return
 cid=int(os.getenv('ATTENDANCE_RESULT_CHANNEL_ID','0') or 0);ch=g.get_channel(cid)
 if ch:
  await ch.send(embed=discord.Embed(title=f'🏆 {month} 출석왕',description=saved[0][0],color=discord.Color.gold()),allowed_mentions=discord.AllowedMentions.none())
  async with lock:
   await db.execute('UPDATE attendance_awards SET posted=1 WHERE guild=? AND month=?',(g.id,month));await db.commit()

voice_clock={}
async def voice_checkpoint(g):
 stamp=time.monotonic(); now=datetime.now(KST); day=now.date().isoformat()
 eligible=set()
 for ch in g.voice_channels:
  if ch==g.afk_channel:continue
  members=[m for m in ch.members if not m.bot and m.voice and not m.voice.self_deaf and not m.voice.deaf]
  if len(members)>=2:eligible.update(m.id for m in members)
 async with lock:
  try:
   await db.execute('BEGIN IMMEDIATE')
   for (gid,u),start in list(voice_clock.items()):
    if gid!=g.id:continue
    sec=max(0,min(90,int(stamp-start)))
    if sec:
     old=await rows('SELECT remainder FROM voice_total WHERE guild=? AND user=?',(gid,u)); rem=(old[0][0] if old else 0)+sec; amount=(rem//360)*5
     await db.execute('INSERT INTO voice_total VALUES(?,?,?,?) ON CONFLICT(guild,user) DO UPDATE SET seconds=seconds+excluded.seconds,remainder=excluded.remainder',(gid,u,sec,rem%360))
     await db.execute('INSERT INTO voice_daily VALUES(?,?,?,?) ON CONFLICT(guild,user,day) DO UPDATE SET seconds=seconds+excluded.seconds',(gid,u,day,sec))
     if amount:await credit_tx(gid,u,amount,'음성 활동 보상',None)
   await db.commit()
  except Exception:await db.rollback();raise
  for k in list(voice_clock):
   if k[0]==g.id:del voice_clock[k]
  voice_clock.update({(g.id,u):stamp for u in eligible})
@bot.event
async def on_voice_state_update(member,before,after):
 if db is not None:await voice_checkpoint(member.guild)
@tasks.loop(seconds=30)
async def economy_tick():
 for g in bot.guilds:
  try:
   await voice_checkpoint(g);await monthly(g,datetime.now(KST))
  except Exception:logging.exception('economy tick failed')
@bot.tree.command(name='음성활동',description='나의 음성 활동 시간')
async def voice_status(inter:discord.Interaction):
 r=await rows('SELECT seconds FROM voice_total WHERE guild=? AND user=?',(inter.guild_id,inter.user.id));s=r[0][0] if r else 0
 await reply(inter,f'누적 음성 활동 {s//3600}시간 {s%3600//60}분 · 시간당 50P')

def safe_icon(g,role):
 return role and not role.managed and not role.is_default() and role<g.me.top_role and role.permissions.value==0 and bool(role.icon)
@bot.tree.command(name='꾸미기등록',description='[관리자] 역할 아이콘 등록')
async def icon_register(inter:discord.Interaction,역할:discord.Role):
 if not await require_admin(inter):return
 if not safe_icon(inter.guild,역할):return await reply(inter,'권한 없는 아이콘 역할만 등록 가능합니다. 봇 역할 순서도 확인하세요.')
 async with lock:
  await db.execute('INSERT INTO icons VALUES(?,?,1) ON CONFLICT(guild,role) DO UPDATE SET active=1',(inter.guild_id,역할.id));await db.commit()
 await reply(inter,'아이콘 등록 완료')
@bot.tree.command(name='꾸미기삭제',description='[관리자] 아이콘 판매 중지')
async def icon_remove(inter:discord.Interaction,역할:discord.Role):
 if not await require_admin(inter):return
 async with lock:
  await db.execute('UPDATE icons SET active=0 WHERE guild=? AND role=?',(inter.guild_id,역할.id));await db.commit()
 await reply(inter,'판매 중지 완료 · 기존 소장 유지')
@bot.tree.command(name='꾸미기상점',description='영구 소장 아이콘 목록')
async def icon_shop(inter:discord.Interaction):
 data=await rows('SELECT role FROM icons WHERE guild=? AND active=1',(inter.guild_id,))
 await reply(inter,('모든 아이콘 50,000P · /꾸미기구매\n'+'\n'.join(f'<@&{r}>' for r, in data))[:1900])
@bot.tree.command(name='꾸미기구매',description='아이콘 영구 소장권 구매')
async def icon_buy(inter:discord.Interaction,역할:discord.Role):
 g,u,r=inter.guild_id,inter.user.id,역할.id
 if not safe_icon(inter.guild,역할):return await reply(inter,'구매할 수 없는 역할입니다.')
 async with lock:
  try:
   await db.execute('BEGIN IMMEDIATE')
   if not await rows('SELECT 1 FROM icons WHERE guild=? AND role=? AND active=1',(g,r)):raise ValueError('판매 중인 아이콘이 아닙니다.')
   if await rows('SELECT 1 FROM icon_owned WHERE guild=? AND user=? AND role=?',(g,u,r)):raise ValueError('이미 소장 중입니다.')
   total=await credit_tx(g,u,-50000,f'아이콘 구매: {역할.name}',None)
   await db.execute('INSERT INTO icon_owned VALUES(?,?,?)',(g,u,r));await db.commit()
  except ValueError as e:await db.rollback();return await reply(inter,str(e))
  except Exception:await db.rollback();raise
 await reply(inter,f'영구 소장 완료 · 잔액 {total:,}P');await post_log(g,u,-50000,total,f'아이콘 구매: {역할.name}',0)
@bot.tree.command(name='꾸미기장착',description='소장 아이콘 장착')
async def icon_equip(inter:discord.Interaction,역할:discord.Role):
 if not safe_icon(inter.guild,역할) or not await rows('SELECT 1 FROM icon_owned WHERE guild=? AND user=? AND role=?',(inter.guild_id,inter.user.id,역할.id)):return await reply(inter,'소장 중인 안전한 아이콘 역할만 장착할 수 있습니다.')
 await inter.response.defer(ephemeral=True)
 async with icon_lock:
  ids={r for r, in await rows('SELECT role FROM icons WHERE guild=?',(inter.guild_id,))}
  member=await inter.guild.fetch_member(inter.user.id)
  await member.edit(roles=[r for r in member.roles if not r.is_default() and r.id not in ids]+[역할],reason='빌지코인 아이콘 장착')
 await reply(inter,'장착 완료')
icon_lock=asyncio.Lock()
@bot.tree.command(name='꾸미기해제',description='장착한 아이콘 해제')
async def icon_unequip(inter:discord.Interaction):
 await inter.response.defer(ephemeral=True)
 async with icon_lock:
  ids={r for r, in await rows('SELECT role FROM icons WHERE guild=?',(inter.guild_id,))}
  member=await inter.guild.fetch_member(inter.user.id)
  roles=[r for r in member.roles if r.id in ids]
  if roles:await member.remove_roles(*roles,reason='빌지코인 아이콘 해제')
 await reply(inter,'해제 완료')
@bot.tree.command(name='인벤토리',description='보유 아이템 및 구매·사용 기록')
async def unified_inventory(inter:discord.Interaction):
 g,u=inter.guild_id,inter.user.id
 items=await rows('SELECT p.name,i.qty FROM inventory i JOIN products p ON p.id=i.product WHERE i.guild=? AND i.user=? AND i.qty>0',(g,u))
 icons=await rows('SELECT role FROM icon_owned WHERE guild=? AND user=?',(g,u))
 requests=await rows('SELECT f.name,r.status,r.reviewed FROM feature_requests r JOIN point_features f ON f.id=r.feature WHERE r.guild=? AND r.user=? ORDER BY r.id DESC LIMIT 15',(g,u))
 em=discord.Embed(title='🎒 통합 인벤토리',color=discord.Color.gold())
 em.add_field(name='보유 상품',value=('\n'.join(f'{n} ×{q}' for n,q in items) or '없음')[:1024],inline=False)
 em.add_field(name='소장 아이콘',value=('\n'.join(f'<@&{r}>' for r, in icons) or '없음')[:1024],inline=False)
 em.add_field(name='기능 신청 및 처리 내역',value=('\n'.join(f'{n} · {s} · {t or "대기"}' for n,s,t in requests) or '없음')[:1024],inline=False)
 await inter.response.send_message(embed=em,ephemeral=True)
@bot.tree.command(name='빌지코인순위',description='포인트 보유량 순위')
async def coin_rank(inter:discord.Interaction):
 data=await rows('SELECT user,points FROM balances WHERE guild=? ORDER BY points DESC LIMIT 20',(inter.guild_id,))
 await reply(inter,'\n'.join(f'{i}위 · <@{u}> · {p:,}P' for i,(u,p) in enumerate(data,1)) or '기록 없음')

# 채널 구성만 변경. 기존 명령어와 보상 로직은 그대로 사용합니다.
async def usage_guide(inter:discord.Interaction):
 await reply(inter,'출석: 선착순 3,000 / 2,000 / 1,000P, 이후 50P\n월간 공동 출석왕: 각 5,000P\n음성 활동: 시간당 50P, 일일 한도 없음\n내전: 일반 1,000P, 단판 300P (지급 조건 충족 시)\n역할 아이콘: 영구 소장 50,000P\n포인트상품 신청: /포인트상품요청')

PANELS={
 'GUIDE_CHANNEL_ID':('빌지코인 이용안내','출석·음성 활동·내전 보상으로 포인트를 모으고 상점에서 사용하세요.',[('이용안내',usage_guide)]),
 'ATTENDANCE_CHANNEL_ID':('출석 체크','매일 자정 · 3,000 / 2,000 / 1,000P · 일반 50P',[('출석 체크',attendance),('월간 순위',attendance_rank),('내 출석',attendance_status)]),
 'FEATURE_SHOP_CHANNEL_ID':('포인트 상점','상품 신청은 /포인트상품요청 · 기존 일반 상점도 조회 가능',[('상품 목록',point_features),('일반 상점',shop)]),
 'ICON_SHOP_CHANNEL_ID':('역할 상점','영구 소장 50,000P · /꾸미기구매 · /꾸미기장착',[('아이콘 목록',icon_shop)]),
 'INVENTORY_CHANNEL_ID':('내 인벤토리','소장 아이템과 기능 신청 내역',[('내 인벤토리',unified_inventory),('아이콘 해제',icon_unequip)]),
 'BALANCE_CHANNEL_ID':('포인트 잔액·내역 조회','잔액과 거래 내역 및 음성 활동 조회 · 시간당 50P',[('잔액 조회',points),('거래 내역',history),('내 음성 활동',voice_status)]),
 'RANK_CHANNEL_ID':('포인트 순위','포인트 및 출석 순위',[('포인트 순위',coin_rank),('출석 순위',attendance_rank)])}
# 기존 음성 패널 버튼도 재시작 후 작동하도록 등록하되 새 패널은 게시하지 않습니다.
LEGACY_VOICE_PANEL=('음성 활동','시간당 50P · 일일 한도 없음',[('내 활동',voice_status)])
def panel_view(key):
 view=discord.ui.View(timeout=None)
 for i,(label,command) in enumerate((LEGACY_VOICE_PANEL if key=='VOICE_CHANNEL_ID' else PANELS[key])[2]):
  button=discord.ui.Button(label=label,custom_id=f'bc:{key}:{i}',style=discord.ButtonStyle.primary)
  async def callback(inter,cmd=command):
   await (cmd.callback if isinstance(cmd,app_commands.Command) else cmd)(inter)
  button.callback=callback;view.add_item(button)
 return view
panels_registered=False
async def publish_panels():
 global panels_registered
 if not panels_registered:bot.add_view(panel_view('VOICE_CHANNEL_ID'))
 for key,(title,desc,_) in PANELS.items():
  view=panel_view(key)
  if not panels_registered:bot.add_view(view)
  cid=int(os.getenv(key,'0') or 0);ch=bot.get_channel(cid)
  if not ch or ch.guild.id!=GUILD_ID:continue
  try:
   em=discord.Embed(title=title,description=desc,color=discord.Color.gold());mid=await setting(GUILD_ID,'panel:'+key)
   if mid:
    try:await (await ch.fetch_message(int(mid))).edit(embed=em,view=view);continue
    except discord.NotFound:pass
   msg=await ch.send(embed=em,view=view);await set_setting(GUILD_ID,'panel:'+key,msg.id)
  except Exception:logging.exception('panel publish failed %s',key)
 panels_registered=True

if __name__=='__main__':asyncio.run(main())
