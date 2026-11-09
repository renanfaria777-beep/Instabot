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

SECRET_KEY = os.getenv("SECRET_KEY", "troca_isso_por_algo_aleatorio_32chars")
ALGORITHM  = "HS256"
DB_URL     = "sqlite:///./bot.db"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

engine   = create_engine(DB_URL, connect_args={"check_same_thread": False})
Session_ = sessionmaker(bind=engine)
Base     = declarative_base()

class User(Base):
    __tablename__ = "users"
    id          = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    email       = Column(String, unique=True, index=True)
    password    = Column(String)
    plan        = Column(String, default="free")
    plan_expiry = Column(DateTime, nullable=True)
    created_at  = Column(DateTime, default=datetime.utcnow)
    is_active   = Column(Boolean, default=True)

class BotAccount(Base):
    __tablename__ = "bot_accounts"
    id              = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id         = Column(String, index=True)
    platform        = Column(String)
    username        = Column(String)
    ig_session      = Column(Text, nullable=True)
    status          = Column(String, default="stopped")
    followers_today = Column(Integer, default=0)
    likes_today     = Column(Integer, default=0)
    comments_today  = Column(Integer, default=0)
    last_action     = Column(DateTime, nullable=True)
    created_at      = Column(DateTime, default=datetime.utcnow)

class BotConfig(Base):
    __tablename__ = "bot_configs"
    id                 = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    account_id         = Column(String, unique=True)
    target_hashtags    = Column(Text, default="[]")
    target_users       = Column(Text, default="[]")
    action_follow      = Column(Boolean, default=True)
    action_like        = Column(Boolean, default=True)
    action_comment     = Column(Boolean, default=False)
    comments_pool      = Column(Text, default='["Incrível! 🔥","Muito bom! 👏","Adorei! ❤️"]')
    daily_follow_limit = Column(Integer, default=50)
    daily_like_limit   = Column(Integer, default=100)
    delay_min_sec      = Column(Integer, default=30)
    delay_max_sec      = Column(Integer, default=90)

class ActionLog(Base):
    __tablename__ = "action_logs"
    id         = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    account_id = Column(String, index=True)
    action     = Column(String)
    target     = Column(String)
    status     = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)

Base.metadata.create_all(engine)

pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
bearer  = HTTPBearer()

def hash_pw(pw): return pwd_ctx.hash(pw)
def verify_pw(pw, h): return pwd_ctx.verify(pw, h)
def create_token(user_id):
    return jwt.encode({"sub": user_id, "exp": datetime.utcnow() + timedelta(days=30)}, SECRET_KEY, algorithm=ALGORITHM)

def get_current_user(creds: HTTPAuthorizationCredentials = Depends(bearer)):
    try:
        payload = jwt.decode(creds.credentials, SECRET_KEY, algorithms=[ALGORITHM])
        uid = payload.get("sub")
    except:
        raise HTTPException(401, "Token inválido")
    db = Session_()
    u  = db.query(User).filter(User.id == uid).first()
    db.close()
    if not u: raise HTTPException(401, "Usuário não encontrado")
    return u

ig_clients = {}

def get_ig_client(account):
    if account.id in ig_clients:
        return ig_clients[account.id]
    if not account.ig_session:
        return None
    cl = IGClient()
    try:
        cl.set_settings(json.loads(account.ig_session))
        cl.get_timeline_feed()
        ig_clients[account.id] = cl
        return cl
    except Exception as e:
        log.error(f"Sessão inválida: {e}")
        return None

def ig_login(account_id, username, password, verification_code=None):
    db  = Session_()
    acc = db.query(BotAccount).filter(BotAccount.id == account_id).first()
    cl  = IGClient()
    cl.delay_range = [2, 5]
    try:
        if verification_code:
            cl.challenge_code = verification_code
        cl.login(username, password)
        acc.ig_session = json.dumps(cl.get_settings())
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

def log_action(db, account_id, action, target, status):
    db.add(ActionLog(account_id=account_id, action=action, target=target, status=status))
    db.commit()

def run_ig_actions(account_id):
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
            if acc.status != "running": break
            medias = cl.hashtag_medias_recent(tag, amount=5)
            for media in medias:
                if acc.status != "running": break
                acc = db.query(BotAccount).filter(BotAccount.id == account_id).first()
                if cfg.action_like and acc.likes_today < cfg.daily_like_limit:
                    try:
                        cl.media_like(media.id)
                        acc.likes_today += 1
                        log_action(db, account_id, "like", str(media.code), "ok")
                    except: log_action(db, account_id, "like", str(media.code), "error")
                if cfg.action_follow and acc.followers_today < cfg.daily_follow_limit:
                    try:
                        cl.user_follow(media.user.pk)
                        acc.followers_today += 1
                        log_action(db, account_id, "follow", media.user.username, "ok")
                    except: log_action(db, account_id, "follow", media.user.username, "error")
                if cfg.action_comment and acc.comments_today < 20 and comments:
                    try:
                        cl.media_comment(media.id, random.choice(comments))
                        acc.comments_today += 1
                        log_action(db, account_id, "comment", str(media.code), "ok")
                    except: pass
                acc.last_action = datetime.utcnow()
                db.commit()
                time.sleep(random.randint(cfg.delay_min_sec, cfg.delay_max_sec))
    except LoginRequired:
        acc.status = "error"
        db.commit()
    except Exception as e:
        log.error(f"Erro: {e}")
    finally:
        db.close()

def reset_daily_counters():
    db = Session_()
    db.query(BotAccount).update({BotAccount.followers_today: 0, BotAccount.likes_today: 0, BotAccount.comments_today: 0})
    db.commit()
    db.close()

scheduler = BackgroundScheduler()
scheduler.add_job(reset_daily_counters, 'cron', hour=0, minute=0)
scheduler.start()

app = FastAPI(title="InstaBot SaaS", docs_url=None)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

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
    comments_pool: List[str] = ["Incrível! 🔥","Muito bom! 👏","Adorei! ❤️"]
    daily_follow_limit: int = 50
    daily_like_limit: int = 100
    delay_min_sec: int = 30
    delay_max_sec: int = 90

@app.post("/auth/register")
def register(data: RegisterSchema):
    db = Session_()
    if db.query(User).filter(User.email == data.email).first():
        db.close()
        raise HTTPException(400, "Email já cadastrado")
    u = User(email=data.email, password=hash_pw(data.password))
    db.add(u); db.commit()
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

@app.get("/accounts")
def list_accounts(user: User = Depends(get_current_user)):
    db   = Session_()
    accs = db.query(BotAccount).filter(BotAccount.user_id == user
