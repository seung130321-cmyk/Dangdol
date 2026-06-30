import discord
from discord.ext import commands
from discord import app_commands
import os
import hashlib
import google.generativeai as genai
from supabase import create_client, Client
from dotenv import load_dotenv
from PIL import Image
import io
import math

load_dotenv()

# ── 환경변수 ──────────────────────────────────────────
DISCORD_TOKEN   = os.environ["DISCORD_TOKEN"]
SUPABASE_URL    = os.environ["SUPABASE_URL"]
SUPABASE_KEY    = os.environ["SUPABASE_KEY"]
GEMINI_API_KEY  = os.environ["GEMINI_API_KEY"]
VERIFIED_ROLE_NAME  = os.environ.get("VERIFIED_ROLE_NAME", "인증됨")
MEMBER_ROLE_NAME    = os.environ.get("MEMBER_ROLE_NAME", "당돌한 맴버")   # 포인트 상점에서 구매
BANNER_LEVELUP      = os.environ.get("BANNER_LEVELUP",  "banners/levelup.png")
BANNER_AI           = os.environ.get("BANNER_AI",       "banners/ai.png")
BANNER_SHOP         = os.environ.get("BANNER_SHOP",     "banners/shop.png")
BANNER_WELCOME      = os.environ.get("BANNER_WELCOME",  "banners/welcome.png")
BANNER_VERIFY       = os.environ.get("BANNER_VERIFY",   "banners/welcome.png")
COMMISSION_PRICE    = int(os.environ.get("COMMISSION_PRICE", "200"))
ROLE_PRICE          = int(os.environ.get("ROLE_PRICE",        "100"))

# ── Supabase ──────────────────────────────────────────
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ── Gemini ────────────────────────────────────────────
genai.configure(api_key=GEMINI_API_KEY)
gemini_model = genai.GenerativeModel("gemini-1.5-flash")

# ── Discord ───────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="py ", intents=intents)
tree = bot.tree


# ═══════════════════════════════════════════════════════
#  유틸리티
# ═══════════════════════════════════════════════════════

def make_hash(user_id: int) -> str:
    """Discord user ID → SHA-256 해시"""
    return hashlib.sha256(str(user_id).encode()).hexdigest()


def level_from_messages(messages: int) -> int:
    """
    누적 메시지 수로 레벨 계산.
    - 1~15레벨: level = floor(sqrt(messages))  (기존과 동일하게 부드러움)
    - 16레벨 이상: 4제곱 기반으로 훨씬 가파르게 증가
    """
    if messages <= 0:
        return 1

    LEVEL_15_MESSAGES = 15 * 15  # 225 (기존 공식으로 15레벨 도달 시점)

    if messages < LEVEL_15_MESSAGES:
        return max(1, math.isqrt(messages))

    # 15레벨 이후: 한 레벨 올리는데 필요한 메시지 수가 4제곱 형태로 증가
    # (20레벨 도달 시점이 약 50,000 메시지가 되도록 계수 조정)
    LEVELUP_SCALE = 80
    extra = messages - LEVEL_15_MESSAGES + 1
    bonus_level = 0
    while LEVELUP_SCALE * (bonus_level + 1) ** 4 <= extra:
        bonus_level += 1
    return 15 + bonus_level


def points_for_levelup(current_level: int) -> int:
    """레벨업 시 부여 포인트 = 현재 레벨^2 - 현재 레벨"""
    return current_level ** 2 - current_level


async def get_or_create_user(user_id: int, username: str) -> dict:
    """Supabase에서 유저 조회 또는 생성"""
    h = make_hash(user_id)
    res = supabase.table("users").select("*").eq("hash", h).execute()
    if res.data:
        return res.data[0]
    new_user = {
        "hash":     h,
        "discord_id": str(user_id),
        "username": username,
        "messages": 0,
        "level":    1,
        "points":   0,
    }
    supabase.table("users").insert(new_user).execute()
    return new_user


async def update_user(hash_val: str, **kwargs):
    supabase.table("users").update(kwargs).eq("hash", hash_val).execute()


def banner_file(path: str) -> discord.File | None:
    if os.path.exists(path):
        return discord.File(path, filename=os.path.basename(path))
    return None


async def send_with_banner(
    target,          # channel / interaction
    banner_path: str,
    embed: discord.Embed,
    *,
    ephemeral: bool = False,
):
    """베너 이미지 + 임베드를 함께 전송"""
    f = banner_file(banner_path)
    if f:
        fname = os.path.basename(banner_path)
        embed.set_image(url=f"attachment://{fname}")
        if isinstance(target, discord.Interaction):
            await target.followup.send(file=f, embed=embed, ephemeral=ephemeral)
        else:
            await target.send(file=f, embed=embed)
    else:
        if isinstance(target, discord.Interaction):
            await target.followup.send(embed=embed, ephemeral=ephemeral)
        else:
            await target.send(embed=embed)


# ═══════════════════════════════════════════════════════
#  인증 버튼 View
# ═══════════════════════════════════════════════════════

class VerifyView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="인증 받기", style=discord.ButtonStyle.success, custom_id="verify_button", emoji="✅")
    async def verify_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        role = discord.utils.get(interaction.guild.roles, name=VERIFIED_ROLE_NAME)
        if not role:
            try:
                role = await interaction.guild.create_role(name=VERIFIED_ROLE_NAME, reason="인증 시스템 자동 생성")
            except discord.Forbidden:
                await interaction.response.send_message("❌ 역할을 생성할 권한이 없어요.", ephemeral=True)
                return

        if role in interaction.user.roles:
            await interaction.response.defer()
            return

        try:
            await interaction.user.add_roles(role, reason="셀프 인증")
            await interaction.response.defer()
        except discord.Forbidden:
            await interaction.response.send_message("❌ 역할을 부여할 권한이 없어요.", ephemeral=True)


# ═══════════════════════════════════════════════════════
#  이벤트: 신규 멤버
# ═══════════════════════════════════════════════════════

@bot.event
async def on_member_join(member: discord.Member):
    # DB 초기화
    await get_or_create_user(member.id, str(member))

    # 첫 번째 텍스트 채널에 환영 메시지
    channel = member.guild.system_channel or next(
        (c for c in member.guild.text_channels if c.permissions_for(member.guild.me).send_messages),
        None,
    )
    if channel:
        embed = discord.Embed(
            description=(
                f"안녕하세요! {member.mention}님, 저는 이 서버의 가이드예요.\n"
                f"도움이 필요하시면 `/도움말`을 입력해 주세요!\n"
                f"아 참, `인증됨` 역할은 #📜┋verify 에서 발급해요! 🎉"
            ),
            color=discord.Color.green(),
        )
        embed.set_author(
            name=member.display_name,
            icon_url=member.display_avatar.url,
        )
        await send_with_banner(channel, BANNER_WELCOME, embed)


# ═══════════════════════════════════════════════════════
#  이벤트: 메시지 → 레벨/포인트 처리
# ═══════════════════════════════════════════════════════

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    await bot.process_commands(message)

    user = await get_or_create_user(message.author.id, str(message.author))
    h = user["hash"]
    new_messages = user["messages"] + 1
    old_level    = user["level"]
    new_level    = level_from_messages(new_messages)
    new_points   = user["points"]

    # 레벨업 처리
    leveled_up = new_level > old_level
    if leveled_up:
        gained = points_for_levelup(new_level)
        new_points += gained

    await update_user(h, messages=new_messages, level=new_level, points=new_points)

    # 레벨업 알림
    if leveled_up:
        embed = discord.Embed(
            title="레벨업! 🎊",
            description=(
                f"{message.author.mention}님이 **레벨 {new_level}**이 되었어요!\n\n"
                f"📩 총 메시지 수: **{new_messages}**\n"
                f"💎 보유 포인트: **{new_points}**\n"
                f"⭐ 현재 레벨: **{new_level}**"
            ),
            color=discord.Color.gold(),
        )
        embed.set_author(
            name=f"축하합니다!",
            icon_url=message.author.display_avatar.url,
        )
        await send_with_banner(message.channel, BANNER_LEVELUP, embed)


# ═══════════════════════════════════════════════════════
#  슬래시 커맨드: /도움말
# ═══════════════════════════════════════════════════════

@tree.command(name="도움말", description="사용 가능한 명령어 목록을 확인해요!")
async def cmd_help(interaction: discord.Interaction):
    await interaction.response.defer()
    embed = discord.Embed(
        title="📋 명령어 목록",
        description=(
            "> `/도움말` — 이 화면을 표시해요.\n"
            "> `/포인트` — 보유 포인트와 상품을 확인해요!\n"
            "> `/질문하기 내용` — AI가 답변해줘요!\n"
        ),
        color=discord.Color.blurple(),
    )
    await send_with_banner(interaction, BANNER_AI, embed)


# ═══════════════════════════════════════════════════════
#  슬래시 커맨드: /포인트
# ═══════════════════════════════════════════════════════

@tree.command(name="포인트", description="보유 포인트와 상품 확인이 가능해요!")
async def cmd_points(interaction: discord.Interaction):
    await interaction.response.defer()
    user = await get_or_create_user(interaction.user.id, str(interaction.user))

    embed = discord.Embed(
        title="💎 포인트 상점",
        color=discord.Color.purple(),
    )
    embed.set_author(
        name=interaction.user.display_name,
        icon_url=interaction.user.display_avatar.url,
    )
    embed.add_field(
        name="내 정보",
        value=(
            f"📩 총 메시지 수: **{user['messages']}**\n"
            f"💎 보유 포인트: **{user['points']}**\n"
            f"⭐ 현재 레벨: **{user['level']}**"
        ),
        inline=False,
    )
    embed.add_field(
        name="🛒 구매 가능 상품",
        value=(
            f"🎨 **커미션** — {COMMISSION_PRICE} 포인트\n"
            f"   `/구매 커미션`으로 구매해요!\n\n"
            f"🏷️ **서버 칭호** (당돌한 맴버 역할) — {ROLE_PRICE} 포인트\n"
            f"   `/구매 칭호`로 구매해요!"
        ),
        inline=False,
    )
    await send_with_banner(interaction, BANNER_SHOP, embed)


# ═══════════════════════════════════════════════════════
#  슬래시 커맨드: /구매
# ═══════════════════════════════════════════════════════

@tree.command(name="구매", description="포인트로 상품을 구매해요!")
@app_commands.describe(상품="구매할 상품을 선택하세요")
@app_commands.choices(상품=[
    app_commands.Choice(name=f"커미션 ({COMMISSION_PRICE}포인트)", value="커미션"),
    app_commands.Choice(name=f"서버 칭호 - 당돌한 맴버 ({ROLE_PRICE}포인트)", value="칭호"),
])
async def cmd_buy(interaction: discord.Interaction, 상품: str):
    await interaction.response.defer(ephemeral=True)
    user = await get_or_create_user(interaction.user.id, str(interaction.user))
    h    = user["hash"]

    price = COMMISSION_PRICE if 상품 == "커미션" else ROLE_PRICE

    if user["points"] < price:
        await interaction.followup.send(
            f"❌ 포인트가 부족해요! (보유: **{user['points']}** / 필요: **{price}**)",
            ephemeral=True,
        )
        return

    new_points = user["points"] - price
    await update_user(h, points=new_points)

    result_msg = ""
    if 상품 == "칭호":
        role = discord.utils.get(interaction.guild.roles, name=MEMBER_ROLE_NAME)
        if not role:
            try:
                role = await interaction.guild.create_role(name=MEMBER_ROLE_NAME, reason="포인트 상점 자동 생성")
            except discord.Forbidden:
                await interaction.followup.send("❌ 역할을 생성할 권한이 없어요.", ephemeral=True)
                return
        try:
            await interaction.user.add_roles(role, reason="포인트 상점 구매")
            result_msg = "✅ **당돌한 맴버** 역할이 지급됐어요!"
        except discord.Forbidden:
            await interaction.followup.send("❌ 역할을 부여할 권한이 없어요.", ephemeral=True)
            return
    else:
        result_msg = "✅ **커미션**을 구매했어요! 관리자에게 DM을 보내주세요."

    await interaction.followup.send(
        f"{result_msg}\n남은 포인트: **{new_points}**",
        ephemeral=True,
    )


# ═══════════════════════════════════════════════════════
#  슬래시 커맨드: /질문하기
# ═══════════════════════════════════════════════════════

@tree.command(name="질문하기", description="AI가 답변을 해줘요!")
@app_commands.describe(내용="AI에게 물어볼 내용을 입력하세요")
async def cmd_ask(interaction: discord.Interaction, 내용: str):
    await interaction.response.defer()

    try:
        response = gemini_model.generate_content(내용)
        answer   = response.text
    except Exception as e:
        answer = f"❌ AI 오류가 발생했어요: {e}"

    embed = discord.Embed(
        title="🤖 AI 답변",
        description=f"**질문:** {내용}\n\n**답변:**\n{answer}",
        color=discord.Color.teal(),
    )
    embed.set_footer(text="Powered by Google Gemini")
    await send_with_banner(interaction, BANNER_AI, embed)


# ═══════════════════════════════════════════════════════
#  슬래시 커맨드: /set (인증 패널)
# ═══════════════════════════════════════════════════════

@tree.command(name="set", description="인증 패널을 이 채널에 설치해요!")
@app_commands.checks.has_permissions(administrator=True)
async def cmd_set(interaction: discord.Interaction):
    await interaction.response.defer()
    embed = discord.Embed(
        description=(
            "### 인증하기\n"
            "⚠️ 주의사항이에요\n\n"
            "`1. 인증하시면 봇이 사용자의 데이터를 수집해요.`\n"
            "`2. 인증하시면 Luma 서버의 규칙을 준수하는 것으로 간주돼요.`\n"
            "`3. 사용자는 테러, 서버 불법 이용, 무단 에셋 배포를 금할 것을 맹세해요.`"
        ),
        color=discord.Color.blue(),
    )
    f = banner_file(BANNER_VERIFY)
    if f:
        fname = os.path.basename(BANNER_VERIFY)
        embed.set_image(url=f"attachment://{fname}")
        await interaction.followup.send(file=f, embed=embed, view=VerifyView())
    else:
        await interaction.followup.send(embed=embed, view=VerifyView())


@cmd_set.error
async def cmd_set_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        if interaction.response.is_done():
            return
        await interaction.response.defer()
        return


# ═══════════════════════════════════════════════════════
#  prefix 커맨드: py 명령어
# ═══════════════════════════════════════════════════════

@bot.command(name="명령어")
async def prefix_help(ctx: commands.Context):
    embed = discord.Embed(
        title="📋 명령어 목록",
        description=(
            "> `/도움말` — 명령어 목록 표시\n"
            "> `/포인트` — 보유 포인트와 상품 확인이 가능해요!\n"
            "> `/질문하기 내용` — AI가 답변을 해줘요!\n"
            "> `/구매 상품` — 포인트로 상품을 구매해요!\n"
            "> `/set` — 인증 패널 설치 (관리자 전용)\n"
            "\n"
            "> `py 명령어` — 이 목록을 표시해요 (prefix 방식)"
        ),
        color=discord.Color.blurple(),
    )
    await ctx.send(embed=embed)


# ═══════════════════════════════════════════════════════
#  봇 시작
# ═══════════════════════════════════════════════════════

@bot.event
async def on_ready():
    bot.add_view(VerifyView())
    await tree.sync()
    print(f"✅ 봇 준비 완료: {bot.user} (ID: {bot.user.id})")


bot.run(DISCORD_TOKEN)
