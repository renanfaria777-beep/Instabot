# main.py — FF Shield Bot · Instagram Automation SaaS
# Python 3.11+ | FastAPI | Instagrapi
#
# pip install fastapi uvicorn instagrapi apscheduler tiktok-api sqlalchemy python-jose passlib
#
# Rodar: uvicorn main:app --host 0.0.0.0 --port 8000
# Deploy: Railway / Render (grátis)

from fastapi import FastAPI, HTTPException, Depends, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
from instagrapi import Client as IGClient
from instagrapi.exceptions import LoginRequired, ChallengeRequired
from sqlalchemy import create_engine, Column, String, Integer, Boolean, DateTime, Text
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from jose import jwt
from passlib.context import CryptContext
import uuid, time, logging, os, json, random

# ─── Config ───────────────────────────────────────────────────
SECRET_KEY  = os.getenv("SECRET_KEY", "troca_isso_por_algo_aleatorio_32chars")
ALGORITHM   = "HS256"
DB_URL      = "sqlite:///./bot.db"
LOG_LEVEL   = logging.INFO

logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ─── Banco de dados ───────────────────────────────────────────
engine  = create_engine(DB_URL, connect_args={"check_same_thread": False})
Session_ = sessionmaker(bind=engine)
Base    = declarative_base()

class User(Base):
    __tablename__ = "users"
    id          = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    email       = Column(String, unique=True, index=True)
    password    = Column(String)
    plan        = Column(String, default="free")   # free | basic | pro
    plan_expiry = Column(DateTime, nullable=True)
    created_at  = Column(DateTime, default=datetime.utcnow)
    is_active   = Column(Boolean, default=True)

class BotAccount(Base):
    __tablename__ = "bot_accounts"
    id           = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id      = Column(String, index=True)
    platform     = Column(String)   # instagram | tiktok
    username     = Column(String)
    ig_session   = Column(Text, nullable=True)   # sessão serializada
    status       = Column(String, default="stopped")  # running | stopped | error
    followers_today = Column(Integer, default=0)
    likes_today     = Column(Integer, default=0)
    comments_today  = Column(Integer, default=0)
    last_action  = Column(DateTime, nullable=True)
    created_at   = Column(DateTime, default=datetime.utcnow)

class BotConfig(Base):
    __tablename__ = "bot_configs"
    id          = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    account_id  = Column(String, unique=True)
    # Instagram
    target_hashtags = Column(Text, default="[]")   # JSON list
    target_users    = Column(Text, default="[]")
    action_follow   = Column(Boolean, default=True)
    action_like     = Column(Boolean, default=True)
    action_comment  = Column(Boolean, default=False)
    comments_pool   = Column(Text, default='["Incrível! 🔥","Muito bom! 👏","Adorei! ❤️"]')
    daily_follow_limit   = Column(Integer, default=50)
    daily_like_limit     = Column(Integer, default=100)
    delay_min_sec        = Column(Integer, default=30)   # delay mínimo entre ações
    delay_max_sec        = Column(Integer, default=90)

class ActionLog(Base):
    __tablename__ = "action_logs"
    id         = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    account_id = Column(String, index=True)
    action     = Column(String)   # follow | like | comment | unfollow
    target     = Column(String)
    status     = Column(String)   # ok | error
    created_at = Column(DateTime, default=datetime.utcnow)

Base.metadata.create_all(engine)

# ─── Auth ─────────────────────────────────────────────────────
pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
bearer  = HTTPBearer()

def hash_pw(pw): return pwd_ctx.hash(pw)
def verify_pw(pw, h): return pwd_ctx.verify(pw, h)

def create_token(user_id: str) -> str:
    return jwt.encode({"sub": user_id, "exp": datetime.utcnow() + timedelta(days=30)},
                      SECRET_KEY, algorithm=ALGORITHM)

def get_current_user(creds: HTTPAuthorizationCredentials = Depends(bearer)):
    try:
        payload = jwt.decode(creds.credentials, SECRET_KEY, algorithms=[ALGORITHM])
        uid = payload.get("sub")
    except Exception:
        raise HTTPException(401, "Token inválido")
    db  = Session_()
    u   = db.query(User).filter(User.id == uid).first()
    db.close()
    if not u: raise HTTPException(401, "Usuário não encontrado")
    return u

# ─── Bot Engine — Instagram ────────────────────────────────────
# Instâncias ativas de clientes IG em memória
ig_clients: dict[str, IGClient] = {}

def get_ig_client(account: BotAccount) -> Optional[IGClient]:
    if account.id in ig_clients:
        return ig_clients[account.id]
    if not account.ig_session:
        return None
    cl = IGClient()
    try:
        cl.set_settings(json.loads(account.ig_session))
        cl.get_timeline_feed()   # testa sessão
        ig_clients[account.id] = cl
        return cl
    except Exception as e:
        log.error(f"Sessão inválida para {account.username}: {e}")
        return None

def ig_login(account_id: str, username: str, password: str, verification_code: str = None) -> dict:
    db = Session_()
    acc = db.query(BotAccount).filter(BotAccount.id == account_id).first()
    cl  = IGClient()
    # Configurações anti-detecção
    cl.delay_range = [2, 5]
    try:
        if verification_code:
            cl.challenge_code = verification_code
        cl.login(username, password)
        session_str = json.dumps(cl.get_settings())
        acc.ig_session = session_str
        acc.status     = "stopped"
        db.commit()
        ig_clients[account_id] = cl
        db.close()
        return {"ok": True}
    except ChallengeRequired:
        db.close()
        return {"ok": False, "reason": "challenge_required"}
    except Exception as e:
        db.close()
        return {"ok": False, "reason": str(e)}

def run_ig_actions(account_id: str):
    db  = Session_()
    acc = db.query(BotAccount).filter(BotAccount.id == account_id).first()
    cfg = db.query(BotConfig).filter(BotConfig.account_id == account_id).first()

    if not acc or not cfg or acc.status != "running":
        db.close()
        return

    cl = get_ig_client(acc)
    if not cl:
        acc.status = "error"
        db.commit()
        db.close()
        return

    hashtags = json.loads(cfg.target_hashtags)
    comments = json.loads(cfg.comments_pool)

    try:
        for tag in hashtags:
            if acc.status != "running":
                break

            medias = cl.hashtag_medias_recent(tag, amount=5)

            for media in medias:
                if acc.status != "running":
                    break

                # Recarrega limites
                acc = db.query(BotAccount).filter(BotAccount.id == account_id).first()

                # Like
                if cfg.action_like and acc.likes_today < cfg.daily_like_limit:
                    try:
                        cl.media_like(media.id)
                        acc.likes_today += 1
                        log_action(db, account_id, "like", str(media.code), "ok")
                        log.info(f"[{acc.username}] Like → {media.code}")
                    except Exception as e:
                        log_action(db, account_id, "like", str(media.code), "error")
                        log.warning(f"Like falhou: {e}")

                # Follow
                if cfg.action_follow and acc.followers_today < cfg.daily_follow_limit:
                    try:
                        cl.user_follow(media.user.pk)
                        acc.followers_today += 1
                        log_action(db, account_id, "follow", media.user.username, "ok")
                        log.info(f"[{acc.username}] Follow → {media.user.username}")
                    except Exception as e:
                        log_action(db, account_id, "follow", media.user.username, "error")
                        log.warning(f"Follow falhou: {e}")

                # Comment
                if cfg.action_comment and acc.comments_today < 20 and comments:
                    try:
                        comment = random.choice(comments)
                        cl.media_comment(media.id, comment)
                        acc.comments_today += 1
                        log_action(db, account_id, "comment", str(media.code), "ok")
                        log.info(f"[{acc.username}] Comment → {media.code}")
                    except Exception as e:
                        log_action(db, account_id, "comment", str(media.code), "error")

                acc.last_action = datetime.utcnow()
                db.commit()

                # Delay humano entre ações
                delay = random.randint(cfg.delay_min_sec, cfg.delay_max_sec)
                time.sleep(delay)

    except LoginRequired:
        acc.status = "error"
        db.commit()
        log.error(f"[{acc.username}] Sessão expirou")
    except Exception as e:
        log.error(f"Erro no bot {acc.username}: {e}")
    finally:
        db.close()

def log_action(db, account_id, action, target, status):
    db.add(ActionLog(account_id=account_id, action=action, target=target, status=status))
    db.commit()

def reset_daily_counters():
    db = Session_()
    db.query(BotAccount).update({
        BotAccount.followers_today: 0,
        BotAccount.likes_today:     0,
        BotAccount.comments_today:  0,
    })
    db.commit()
    db.close()
    log.info("Contadores diários resetados")

# ─── Scheduler ────────────────────────────────────────────────
scheduler = BackgroundScheduler()
scheduler.add_job(reset_daily_counters, 'cron', hour=0, minute=0)

def schedule_active_bots():
    db   = Session_()
    accs = db.query(BotAccount).filter(BotAccount.status == "running").all()
    for acc in accs:
        scheduler.add_job(
            run_ig_actions,
            'interval',
            minutes=15,
            id=f"bot_{acc.id}",
            args=[acc.id],
            replace_existing=True
        )
    db.close()

scheduler.start()

# ─── FastAPI ──────────────────────────────────────────────────
app = FastAPI(title="InstaBot SaaS", docs_url=None)
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])

# ── Schemas ──
class RegisterSchema(BaseModel):
    email: str
    password: str

class LoginSchema(BaseModel):
    email: str
    password: str

class IGLoginSchema(BaseModel):
    account_id: str
    username: str
    password: str
    verification_code: Optional[str] = None

class ConfigSchema(BaseModel):
    account_id: str
    target_hashtags: List[str] = []
    target_users: List[str] = []
    action_follow: bool = True
    action_like: bool = True
    action_comment: bool = False
    comments_pool: List[str] = ["Incrível! 🔥", "Muito bom! 👏", "Adorei! ❤️"]
    daily_follow_limit: int = 50
    daily_like_limit: int = 100
    delay_min_sec: int = 30
    delay_max_sec: int = 90

# ── Auth routes ──
@app.post("/auth/register")
def register(data: RegisterSchema):
    db = Session_()
    if db.query(User).filter(User.email == data.email).first():
        db.close()
        raise HTTPException(400, "Email já cadastrado")
    u = User(email=data.email, password=hash_pw(data.password))
    db.add(u)
    db.commit()
    token = create_token(u.id)
    db.close()
    return {"token": token}

@app.post("/auth/login")
def login(data: LoginSchema):
    db = Session_()
    u  = db.query(User).filter(User.email == data.email).first()
    db.close()
    if not u or not verify_pw(data.password, u.password):
        raise HTTPException(401, "Credenciais inválidas")
    return {"token": create_token(u.id), "plan": u.plan}

# ── Accounts ──
@app.get("/accounts")
def list_accounts(user: User = Depends(get_current_user)):
    db   = Session_()
    accs = db.query(BotAccount).filter(BotAccount.user_id == user.id).all()
    db.close()
    return [{"id": a.id, "platform": a.platform, "username": a.username,
             "status": a.status, "followers_today": a.followers_today,
             "likes_today": a.likes_today, "last_action": a.last_action} for a in accs]

@app.post("/accounts/add")
def add_account(platform: str, user: User = Depends(get_current_user)):
    db  = Session_()
    acc = BotAccount(user_id=user.id, platform=platform, username="")
    db.add(acc)
    cfg = BotConfig(account_id=acc.id)
    db.add(cfg)
    db.commit()
    acc_id = acc.id
    db.close()
    return {"account_id": acc_id}

@app.post("/accounts/ig-login")
def ig_login_route(data: IGLoginSchema, user: User = Depends(get_current_user)):
    db  = Session_()
    acc = db.query(BotAccount).filter(
        BotAccount.id == data.account_id,
        BotAccount.user_id == user.id
    ).first()
    if not acc:
        db.close()
        raise HTTPException(404, "Conta não encontrada")
    acc.username = data.username
    db.commit()
    db.close()
    result = ig_login(data.account_id, data.username, data.password, data.verification_code)
    return result

# ── Config ──
@app.post("/config/save")
def save_config(data: ConfigSchema, user: User = Depends(get_current_user)):
    db  = Session_()
    cfg = db.query(BotConfig).filter(BotConfig.account_id == data.account_id).first()
    if not cfg:
        cfg = BotConfig(account_id=data.account_id)
        db.add(cfg)
    cfg.target_hashtags      = json.dumps(data.target_hashtags)
    cfg.action_follow        = data.action_follow
    cfg.action_like          = data.action_like
    cfg.action_comment       = data.action_comment
    cfg.comments_pool        = json.dumps(data.comments_pool)
    cfg.daily_follow_limit   = data.daily_follow_limit
    cfg.daily_like_limit     = data.daily_like_limit
    cfg.delay_min_sec        = data.delay_min_sec
    cfg.delay_max_sec        = data.delay_max_sec
    db.commit()
    db.close()
    return {"ok": True}

# ── Bot control ──
@app.post("/bot/start/{account_id}")
def start_bot(account_id: str, user: User = Depends(get_current_user)):
    db  = Session_()
    acc = db.query(BotAccount).filter(
        BotAccount.id == account_id,
        BotAccount.user_id == user.id
    ).first()
    if not acc: raise HTTPException(404)
    if not acc.ig_session:
        db.close()
        raise HTTPException(400, "Faça login no Instagram primeiro")
    acc.status = "running"
    db.commit()
    db.close()
    scheduler.add_job(run_ig_actions, 'interval', minutes=15,
                      id=f"bot_{account_id}", args=[account_id],
                      replace_existing=True)
    # Roda imediatamente também
    scheduler.add_job(run_ig_actions, args=[account_id])
    return {"ok": True, "status": "running"}

@app.post("/bot/stop/{account_id}")
def stop_bot(account_id: str, user: User = Depends(get_current_user)):
    db  = Session_()
    acc = db.query(BotAccount).filter(
        BotAccount.id == account_id,
        BotAccount.user_id == user.id
    ).first()
    if not acc: raise HTTPException(404)
    acc.status = "stopped"
    db.commit()
    db.close()
    try: scheduler.remove_job(f"bot_{account_id}")
    except: pass
    return {"ok": True, "status": "stopped"}

@app.get("/bot/logs/{account_id}")
def get_logs(account_id: str, limit: int = 50, user: User = Depends(get_current_user)):
    db   = Session_()
    logs = db.query(ActionLog).filter(
        ActionLog.account_id == account_id
    ).order_by(ActionLog.created_at.desc()).limit(limit).all()
    db.close()
    return [{"action": l.action, "target": l.target,
             "status": l.status, "time": l.created_at} for l in logs]

@app.get("/stats/{account_id}")
def get_stats(account_id: str, user: User = Depends(get_current_user)):
    db  = Session_()
    acc = db.query(BotAccount).filter(BotAccount.id == account_id).first()
    db.close()
    if not acc: raise HTTPException(404)
    return {
        "followers_today": acc.followers_today,
        "likes_today":     acc.likes_today,
        "comments_today":  acc.comments_today,
        "status":          acc.status,
        "last_action":     acc.last_action,
    }
