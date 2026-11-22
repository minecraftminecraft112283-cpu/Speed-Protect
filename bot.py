"""SPEED PROTECT Discord bot providing a persistent /setpanel command."""
from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime
from typing import TypeAlias
from urllib.parse import urlsplit

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

# Token kodun icinde saxlanmir; sayt/API ve ya environment deyisenlerinden goturulur.
SITE_BASE_URL = (
    os.getenv("SITE_BASE_URL")
    or os.getenv("PUBLIC_SITE_URL")
    or "https://speedprotect-urskz6i8.manus.space"
).rstrip("/")
BOT_TOKEN_ENV_KEYS = ("DISCORD_BOT_TOKEN", "BOT_TOKEN", "SPEED_BOT_TOKEN")
API_TOKEN_ENV_KEYS = ("BOT_API_TOKEN", "SITE_BOT_TOKEN", "DISCORD_BOT_TOKEN", "BOT_TOKEN")

LOG = logging.getLogger("speed_protect_bot")
PAGE_SIZE = 25
MAX_KEY_AMOUNT = 100
PLANSKEY_ADMIN_ID = 1446508099170205782
MAX_DURATION_SECONDS = 36500 * 24 * 60 * 60
DURATION_PATTERN = re.compile(r"^(0|([1-9]\d{0,4})(m|s|d|y))$", re.IGNORECASE)
DURATION_MULTIPLIERS = {
    "m": 60,
    "s": 1,
    "d": 24 * 60 * 60,
    "y": 365 * 24 * 60 * 60,
}
PanelChannel: TypeAlias = discord.TextChannel | discord.Thread


class BotApiError(RuntimeError):
    def __init__(self, status: int, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def parse_duration(value: str) -> int:
    match = DURATION_PATTERN.fullmatch(value.strip()) if isinstance(value, str) else None
    if match is None:
        raise ValueError("Use 0 for lifetime, or a duration such as 1m, 1s, 1d, or 1y.")
    amount = 0 if match.group(1) == "0" else int(match.group(2))
    unit = (match.group(3) or "").lower()
    duration_seconds = 0 if amount == 0 else amount * DURATION_MULTIPLIERS[unit]
    if duration_seconds > MAX_DURATION_SECONDS:
        raise ValueError("The maximum key duration is 100 years.")
    return duration_seconds


def validate_site_base_url(raw_url: str) -> str:
    parsed = urlsplit(raw_url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("SITE_BASE_URL must be an HTTPS origin without credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("SITE_BASE_URL must not contain a query or fragment")
    if parsed.path not in ("", "/"):
        raise ValueError("SITE_BASE_URL must be an origin, without a path")
    return f"https://{parsed.netloc}"


def _read_first_env(keys: tuple[str, ...]) -> str:
    for key in keys:
        val = (os.getenv(key) or "").strip()
        if val:
            return val
    return ""


async def resolve_tokens_from_site(site_base_url: str) -> tuple[str, str]:
    """Sayt API-sindən bot tokeni və API tokeni çəkir (əgər env-də yoxdursa)."""
    env_bot_token = _read_first_env(BOT_TOKEN_ENV_KEYS)
    env_api_token = _read_first_env(API_TOKEN_ENV_KEYS)

    if env_bot_token and env_api_token:
        return env_bot_token, env_api_token

    timeout = aiohttp.ClientTimeout(total=15)
    headers: dict[str, str] = {}
    if env_api_token:
        headers["Authorization"] = f"Bot {env_api_token}"

    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(f"{site_base_url}/api/bot/config") as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    if isinstance(data, dict):
                        site_bot_token = str(data.get("botToken") or data.get("discordBotToken") or "").strip()
                        site_api_token = str(data.get("apiToken") or data.get("botApiToken") or site_bot_token).strip()
                        final_bot = env_bot_token or site_bot_token
                        final_api = env_api_token or site_api_token or final_bot
                        if final_bot:
                            return final_bot, final_api
    except Exception as exc:
        LOG.warning("Site config token fetch failed (%s)", type(exc).__name__)

    fallback_bot = env_bot_token or env_api_token
    return fallback_bot, (env_api_token or fallback_bot)


def member_role_rank(guild: discord.Guild, member: discord.Member) -> int | None:
    """Return the member's highest human-assignable server-role rank (1 is highest)."""
    default_role_id = guild.default_role.id
    ranked_roles = sorted(
        (
            role for role in guild.roles
            if role.id != default_role_id and not role.managed
        ),
        key=lambda role: (role.position, -role.id),
        reverse=True,
    )
    member_role_ids = {
        role.id for role in member.roles
        if role.id != default_role_id and not role.managed
    }
    return next(
        (rank for rank, role in enumerate(ranked_roles, start=1) if role.id in member_role_ids),
        None,
    )


LICENSE_KEY_PATTERN = re.compile(
    r"^(?:(?=.*[A-Z])(?=.*[a-z])(?=.*[0-9])[A-Za-z0-9]{25}|[A-HJ-NP-Z2-9]{25}|[A-HJ-NP-Z2-9]{16}|SP-[A-F0-9]{48})$"
)
PLAN_KEY_PATTERN = re.compile(
    r"^SP-(?:PREMIUM|ULTRA)-(?=[A-Za-z0-9]{25}$)(?=.*[A-Z])(?=.*[a-z])(?=.*[0-9])[A-Za-z0-9]{25}$"
)
LOADER_PATH_PATTERN = re.compile(
    r"^(?:/api/loaders/([A-Za-z0-9_-]{43})/source\?key=|/files/v4/loaders/([A-Za-z0-9_-]{43})\.lua)$"
)


def build_loader_line(
    site_base_url: str,
    source_path: str,
    license_key: str | None,
    free_mode: bool = False,
) -> str:
    match = LOADER_PATH_PATTERN.fullmatch(source_path)
    if not match:
        raise BotApiError(502, "The site API returned an invalid loader path.")
    token = match.group(1) or match.group(2)
    url = f"{site_base_url.rstrip('/')}/files/v4/loaders/{token}.lua"
    if free_mode:
        return f'loadstring(game:HttpGet("{url}"))()'
    key_value = (
        license_key.strip()
        if isinstance(license_key, str) and LICENSE_KEY_PATTERN.fullmatch(license_key.strip())
        else "KEY"
    )
    return (
        f'script_key = "{key_value}";\n'
        f'loadstring(game:HttpGet("{url}"))()'
    )


def panel_description_for(script_title: str) -> str:
    safe_title = discord.utils.escape_markdown(
        discord.utils.escape_mentions(str(script_title))
    )
    return (
        f"This control panel is for the project: **{safe_title}**\n"
        "If you're a buyer, click on the buttons below to redeem your key."
    )


def panel_embed(panel_name: str, script_title: str) -> discord.Embed:
    return discord.Embed(
        title=panel_name,
        description=panel_description_for(script_title),
        color=discord.Color(0x4D92FF),
    )


def not_whitelisted_embed() -> discord.Embed:
    return discord.Embed(
        title="Not Whitelisted",
        description=(
            "You need to be whitelisted to get this script.\n"
            "If you have a script key, click on the Redeem button below to redeem it"
        ),
        color=discord.Color(0xE74C3C),
    )


def not_whitelisted_stats_embed() -> discord.Embed:
    return discord.Embed(
        title="Not Whitelisted",
        description=(
            "You need to be whitelisted to view your stats.\n"
            "If you have a script key, click on the Redeem button below to redeem it"
        ),
        color=discord.Color(0xE74C3C),
    )


def key_status_embed(stats: dict) -> discord.Embed:
    license_key = str(stats.get("licenseKey", ""))
    masked_key = f"{license_key[:5]}********" if license_key else "********"
    hwid_status = "Bound" if stats.get("hwid") else "Not bound yet"
    expiry_value = stats.get("expiresAt")
    if expiry_value:
        expires_at = datetime.fromisoformat(str(expiry_value).replace("Z", "+00:00"))
        expiry_text = f"<t:{int(expires_at.timestamp())}:F>"
    else:
        expiry_text = "Lifetime"
    return discord.Embed(
        title="📊 YOUR KEY STATUS",
        description=(
            f"• **Status:** ✅ {stats.get('status', 'Active')}\n"
            f"• **HWID:** {hwid_status}\n"
            f"• **Expiry:** {expiry_text}\n"
            f"• **Key:** `{masked_key}`"
        ),
        color=discord.Color(0x2ECC71),
    )


def hwid_reset_embed(message: str) -> discord.Embed:
    return discord.Embed(
        title="HWID Reset",
        description=message,
        color=discord.Color(0x2ECC71),
    )


def hwid_reset_disabled_embed() -> discord.Embed:
    return discord.Embed(
        title="HWID Reset Disabled",
        description="Self-service HWID reset is disabled for this script by its owner. Contact them for help.",
        color=discord.Color(0xE74C3C),
    )


def free_mode_embed(result: dict) -> discord.Embed:
    enabled = result.get("enabled") is True
    if result.get("changed") is False:
        message = (
            "This script already enabled free mode."
            if enabled else "This script already disabled free mode."
        )
    else:
        title = discord.utils.escape_markdown(
            discord.utils.escape_mentions(str(result.get("title", "this script")))
        )
        message = f"Free Mode {'enabled' if enabled else 'disabled'} for **{title}**."
    return discord.Embed(
        title="Free Mode",
        description=message,
        color=discord.Color(0x2ECC71 if enabled else 0xF1C40F),
    )


def key_redeemed_embed(user_id: int) -> discord.Embed:
    return discord.Embed(
        title="Key Redeemed Successfully",
        description=f"<@{user_id}> has been whitelisted.",
        color=discord.Color(0x2ECC71),
    )


def blacklisted_embed() -> discord.Embed:
    return discord.Embed(
        title="You are blacklisted from this script",
        description="You cannot use this script while you are blacklisted.",
        color=discord.Color(0xE74C3C),
    )


class MobileCopyView(discord.ui.View):
    """A mobile-friendly second step that sends the loader as a normal copyable message."""

    def __init__(self, loader: str) -> None:
        super().__init__(timeout=300)
        self.loader = loader
        button = discord.ui.Button(
            label="Mobile Copy",
            emoji="📱",
            style=discord.ButtonStyle.primary,
            custom_id="speedprotect:mobile-copy:v1",
        )
        button.callback = self.mobile_copy
        self.add_item(button)

    async def mobile_copy(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(self.loader, ephemeral=True)


class RedeemKeyModal(discord.ui.Modal):
    def __init__(self, bot: "SpeedProtectBot", guild_id: int, channel_id: int, message_id: int) -> None:
        super().__init__(title="Redeem Key", timeout=300)
        self.bot = bot
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.message_id = message_id
        self.key_input = discord.ui.TextInput(
            label="Key",
            placeholder="Paste your key here",
            min_length=5,
            max_length=60,
            required=True,
        )
        self.add_item(self.key_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id != self.guild_id or interaction.channel_id != self.channel_id:
            await interaction.response.send_message(
                "Submit this key from the same server channel where you opened the form.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            result = await self.bot.redeem_key(
                guild_id=self.guild_id,
                channel_id=self.channel_id,
                message_id=self.message_id,
                user_id=interaction.user.id,
                raw_key=self.key_input.value,
            )
            expiry_value = result.get("expiresAt")
            expiry_text = "Lifetime"
            if expiry_value:
                expiry = datetime.fromisoformat(expiry_value.replace("Z", "+00:00"))
                expiry_text = f"<t:{int(expiry.timestamp())}:F>"
        except BotApiError as error:
            if error.status == 403 and error.code == "blacklisted":
                await interaction.edit_original_response(content=None, embed=blacklisted_embed(), view=None)
                return
            if error.status == 403 and error.code == "key_disabled":
                message = "That key was cancelled by the script owner."
            elif error.status == 403 and error.code == "script_disabled":
                message = "This script is currently disabled by its owner. Check back later."
            elif error.status in (400, 404, 409):
                message = "That key is invalid, has already been used, or is for another script."
            else:
                message = "Could not redeem that key right now. Try again shortly."
            await interaction.edit_original_response(content=message)
            return
        except Exception as error:
            LOG.warning("key_redeem_failed (%s)", type(error).__name__)
            await interaction.edit_original_response(content="Could not redeem that key right now. Try again shortly.")
            return

        try:
            await interaction.followup.send(
                embed=key_redeemed_embed(interaction.user.id),
                allowed_mentions=discord.AllowedMentions(
                    users=[interaction.user], roles=False, everyone=False,
                ),
            )
        except discord.HTTPException as error:
            LOG.warning("key_redeemed_announcement_failed (%s)", type(error).__name__)
        await interaction.edit_original_response(
            content=f"Key redeemed. Your access is active until {expiry_text}."
        )


class PanelButtons(discord.ui.View):
    """Persistent panel controls for key redemption and authorized script access."""

    def __init__(self, bot: "SpeedProtectBot") -> None:
        super().__init__(timeout=None)
        self.bot = bot
        redeem = discord.ui.Button(
            label="Redeem Key", emoji="🔑", style=discord.ButtonStyle.success,
            custom_id="speedprotect:redeem:v1", row=0,
        )
        redeem.callback = self.redeem_key
        self.add_item(redeem)

        get_script = discord.ui.Button(
            label="Get Script", emoji="👑", style=discord.ButtonStyle.primary,
            custom_id="speedprotect:get-script:v1", row=0,
        )
        get_script.callback = self.get_script
        self.add_item(get_script)

        reset_hwid = discord.ui.Button(
            label="Reset HWID", emoji="⚙️", style=discord.ButtonStyle.secondary,
            custom_id="speedprotect:reset-hwid:v1", row=1,
        )
        reset_hwid.callback = self.reset_hwid
        self.add_item(reset_hwid)

        get_stats = discord.ui.Button(
            label="Get Stats", emoji="📊", style=discord.ButtonStyle.secondary,
            custom_id="speedprotect:get-stats:v1", row=1,
        )
        get_stats.callback = self.get_stats
        self.add_item(get_stats)

    async def redeem_key(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None or interaction.channel_id is None or interaction.message is None:
            await interaction.response.send_message(
                "This panel could not be verified. Ask an admin to run `/setpanel` again.",
                ephemeral=True,
            )
            return
        try:
            blocked = await self.bot.check_blacklist(
                interaction.guild_id, interaction.channel_id, interaction.message.id, interaction.user.id
            )
            if blocked:
                await interaction.response.send_message(embed=blacklisted_embed(), ephemeral=True)
                return
        except BotApiError:
            await interaction.response.send_message(
                "Could not verify this key panel right now. Please try again shortly.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(RedeemKeyModal(
            self.bot, guild_id=interaction.guild_id, channel_id=interaction.channel_id, message_id=interaction.message.id,
        ))

    async def get_script(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None or interaction.channel_id is None or interaction.message is None:
            await interaction.response.send_message(
                "This panel could not be verified. Ask an admin to run `/setpanel` again.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            access = await self.bot.get_script_access(
                guild_id=interaction.guild_id,
                channel_id=interaction.channel_id,
                message_id=interaction.message.id,
                user_id=interaction.user.id,
            )
        except BotApiError as error:
            if error.status == 403 and error.code == "blacklisted":
                await interaction.edit_original_response(content=None, embed=blacklisted_embed(), view=None)
            elif error.status == 403 and error.code == "script_disabled":
                await interaction.edit_original_response(
                    content="This script is currently disabled by its owner. Check back later.",
                    embed=None, view=None,
                )
            elif error.status == 403 and error.code == "not_whitelisted":
                await interaction.edit_original_response(
                    content=None, embed=not_whitelisted_embed(), view=None,
                )
            elif error.status == 404:
                await interaction.edit_original_response(
                    content="This panel is no longer available. Ask an admin to run `/setpanel` again.",
                    embed=None, view=None,
                )
            else:
                await interaction.edit_original_response(
                    content="Could not check whitelist access right now. Try again shortly.",
                    embed=None, view=None,
                )
            return
        except Exception as error:
            LOG.warning("script_access_failed (%s)", type(error).__name__)
            await interaction.edit_original_response(
                content="Could not check whitelist access right now. Try again shortly.",
                embed=None, view=None,
            )
            return

        expiry_value = access.get("expiresAt")
        expiry_text = "Lifetime"
        if expiry_value:
            expiry = datetime.fromisoformat(expiry_value.replace("Z", "+00:00"))
            expiry_text = f"<t:{int(expiry.timestamp())}:F>"
        loader = build_loader_line(
            self.bot.site_base_url,
            access["sourcePath"],
            access.get("licenseKey"),
            free_mode=access.get("freeMode") is True,
        )
        note = "Free Mode is enabled. Paste this into your executor." if access.get("freeMode") is True else "Paste this into your executor."
        expiry_suffix = " No key required." if access.get("freeMode") is True else f" Access active until {expiry_text}."
        await interaction.edit_original_response(
            content=f"{note}\n```lua\n{loader}\n```{expiry_suffix}",
            embed=None,
            view=MobileCopyView(loader),
        )

    async def get_stats(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None or interaction.channel_id is None or interaction.message is None:
            await interaction.response.send_message(
                "This panel could not be verified. Ask an admin to run `/setpanel` again.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            stats = await self.bot.get_key_stats(
                guild_id=interaction.guild_id,
                channel_id=interaction.channel_id,
                message_id=interaction.message.id,
                user_id=interaction.user.id,
            )
        except BotApiError as error:
            if error.status == 403 and error.code == "blacklisted":
                await interaction.edit_original_response(content=None, embed=blacklisted_embed(), view=None)
            elif error.status == 403 and error.code == "not_whitelisted":
                await interaction.edit_original_response(
                    content=None, embed=not_whitelisted_stats_embed(), view=None,
                )
            else:
                await interaction.edit_original_response(
                    content="Could not load your key stats right now. Try again shortly.",
                    embed=None, view=None,
                )
            return
        except Exception as error:
            LOG.warning("key_stats_failed (%s)", type(error).__name__)
            await interaction.edit_original_response(
                content="Could not load your key stats right now. Try again shortly.",
                embed=None, view=None,
            )
            return
        await interaction.edit_original_response(content=None, embed=key_status_embed(stats), view=None)

    async def reset_hwid(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None or interaction.channel_id is None or interaction.message is None:
            await interaction.response.send_message(
                "This panel could not be verified. Ask an admin to run `/setpanel` again.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            result = await self.bot.reset_hwid(
                guild_id=interaction.guild_id,
                channel_id=interaction.channel_id,
                message_id=interaction.message.id,
                user_id=interaction.user.id,
            )
        except BotApiError as error:
            if error.status == 403 and error.code == "blacklisted":
                await interaction.edit_original_response(content=None, embed=blacklisted_embed(), view=None)
            elif error.status == 403 and error.code == "hwid_reset_disabled":
                await interaction.edit_original_response(
                    content=None, embed=hwid_reset_disabled_embed(), view=None,
                )
            elif error.status == 403 and error.code == "not_whitelisted":
                await interaction.edit_original_response(
                    content=None, embed=not_whitelisted_embed(), view=None,
                )
            elif error.status == 429 and error.code == "hwid_cooldown":
                await interaction.edit_original_response(content=str(error), embed=None, view=None)
            else:
                await interaction.edit_original_response(
                    content="Could not reset your HWID right now. Try again shortly.",
                    embed=None, view=None,
                )
            return
        except Exception as error:
            LOG.warning("hwid_reset_failed (%s)", type(error).__name__)
            await interaction.edit_original_response(
                content="Could not reset your HWID right now. Try again shortly.",
                embed=None, view=None,
            )
            return
        await interaction.edit_original_response(
            content=None,
            embed=hwid_reset_embed(result.get("message", "Your HWID has been reset.")),
            view=None,
        )


class ScriptSelect(discord.ui.Select):
    def __init__(self, owner_view: "ScriptSelectionView", rows: list[dict]) -> None:
        self.owner_view = owner_view
        options = [
            discord.SelectOption(
                label=f"{row['title']} — {row['filename']}"[:100],
                value=str(row["id"]),
                description=f"Uploaded script · {row['filename']}"[:100],
            )
            for row in rows
        ]
        super().__init__(
            placeholder="Choose a script",
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.owner_view
        await interaction.response.defer(ephemeral=True)
        try:
            loader = await view.bot.get_owned_loader(view.owner_id, self.values[0])
            if loader is None:
                await interaction.edit_original_response(
                    content="That script is no longer available. Run the command again and choose another script.",
                    view=None,
                )
                return
            if view.key_duration_seconds is not None:
                keys = await view.bot.create_keys(
                    owner_id=view.owner_id,
                    loader_id=loader["id"],
                    duration_seconds=view.key_duration_seconds,
                    amount=view.key_amount,
                )
                await view.bot.send_generated_keys(
                    interaction,
                    keys=keys,
                    loader=loader,
                    duration_label=view.duration_label,
                )
                return
            if view.cooldown_duration_seconds is not None:
                await view.bot.set_hwid_cooldown(
                    owner_id=view.owner_id,
                    loader_id=loader["id"],
                    duration_seconds=view.cooldown_duration_seconds,
                )
                label = "lifetime" if view.cooldown_duration_seconds == 0 else view.duration_label
                await interaction.edit_original_response(
                    content=f"HWID cooldown for **{loader['title']}** set to **{label}**.",
                    view=None,
                )
                return
            if view.free_mode_enabled is not None:
                result = await view.bot.set_free_mode(
                    owner_id=view.owner_id,
                    loader_id=loader["id"],
                    enabled=view.free_mode_enabled,
                )
                await interaction.edit_original_response(content=None, embed=free_mode_embed(result), view=None)
                return
            if view.membership_action is not None:
                if view.membership_action == "whitelist":
                    result = await view.bot.whitelist_users(
                        owner_id=view.owner_id,
                        loader_id=loader["id"],
                        user_ids=view.membership_user_ids,
                        duration_seconds=view.membership_duration_seconds,
                    )
                    panel = await view.bot.get_panel_for_script(view.guild_id, view.owner_id, loader["id"])
                    await view.bot.post_whitelist_message(
                        interaction,
                        view.channel,
                        view.membership_target_mention,
                        view.membership_members,
                        result,
                        loader["title"],
                        view.bot.panel_message_url(view.guild_id, panel),
                    )
                    content = f"Whitelisted {len(result['keys'])} user(s) for **{loader['title']}**."
                elif view.membership_action == "unwhitelist":
                    result = await view.bot.unwhitelist_users(
                        owner_id=view.owner_id, loader_id=loader["id"], user_ids=view.membership_user_ids,
                    )
                    content = f"Unwhitelisted {len(view.membership_user_ids)} user(s) from **{loader['title']}** ({result['removed']} key(s) revoked)."
                elif view.membership_action == "blacklist":
                    await view.bot.blacklist_users(view.owner_id, loader["id"], view.membership_user_ids)
                    content = f"Blacklisted {len(view.membership_user_ids)} user(s) from **{loader['title']}**."
                else:
                    await view.bot.unblacklist_users(view.owner_id, loader["id"], view.membership_user_ids)
                    content = f"Unblacklisted {len(view.membership_user_ids)} user(s) from **{loader['title']}**."
                await interaction.edit_original_response(content=content, view=None)
                return
            await view.bot.publish_panel(
                channel=view.channel,
                guild_id=view.guild_id,
                owner_id=view.owner_id,
                loader=loader,
                panel_name=view.panel_name,
            )
        except Exception as error:
            LOG.warning("script_select_failed (%s)", type(error).__name__)
            await interaction.edit_original_response(
                content="Could not complete that action right now. Please try again shortly.",
                view=None,
            )
            return

        await interaction.edit_original_response(
            content=f"Panel posted in <#{view.channel.id}>.",
            view=None,
        )


class ScriptSelectionView(discord.ui.View):
    """Ephemeral, owner-only script picker with pagination for more than 25 scripts."""

    def __init__(
        self,
        bot: "SpeedProtectBot",
        channel: PanelChannel,
        guild_id: int,
        owner_id: int,
        panel_name: str,
        total: int,
        rows: list[dict],
        key_duration_seconds: int | None = None,
        duration_label: str | None = None,
        key_amount: int = 1,
        cooldown_duration_seconds: int | None = None,
        membership_action: str | None = None,
        membership_members: list[discord.Member] | None = None,
        membership_duration_seconds: int | None = None,
        membership_target_mention: str | None = None,
        free_mode_enabled: bool | None = None,
    ) -> None:
        super().__init__(timeout=300)
        self.bot = bot
        self.channel = channel
        self.guild_id = guild_id
        self.owner_id = owner_id
        self.panel_name = panel_name
        self.total = total
        self.key_duration_seconds = key_duration_seconds
        self.cooldown_duration_seconds = cooldown_duration_seconds
        self.membership_action = membership_action
        self.membership_members = membership_members or []
        self.membership_user_ids = [str(member.id) for member in self.membership_members]
        self.membership_duration_seconds = membership_duration_seconds
        self.membership_target_mention = membership_target_mention
        self.free_mode_enabled = free_mode_enabled
        self.duration_label = duration_label
        self.key_amount = key_amount
        self.page = 0
        self.select_menu: ScriptSelect | None = None
        self._set_items(rows)

    def _set_items(self, rows: list[dict]) -> None:
        self.clear_items()
        self.select_menu = ScriptSelect(self, rows)
        self.add_item(self.select_menu)
        if self.total > PAGE_SIZE:
            previous = discord.ui.Button(
                label="Previous", style=discord.ButtonStyle.secondary,
                disabled=self.page == 0, row=1,
            )
            next_button = discord.ui.Button(
                label="Next", style=discord.ButtonStyle.secondary,
                disabled=(self.page + 1) * PAGE_SIZE >= self.total, row=1,
            )
            previous.callback = self.previous_page
            next_button.callback = self.next_page
            self.add_item(previous)
            self.add_item(next_button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the person who ran this command can choose the script.",
                ephemeral=True,
            )
            return False
        return True

    async def _change_page(self, interaction: discord.Interaction, delta: int) -> None:
        self.page = max(0, min(self.page + delta, (self.total - 1) // PAGE_SIZE))
        await interaction.response.defer()
        rows = await self.bot.get_owned_loader_page(self.owner_id, self.page)
        if not rows:
            await interaction.edit_original_response(content="No scripts are available on this page.", view=None)
            return
        self._set_items(rows)
        await interaction.edit_original_response(view=self)

    async def previous_page(self, interaction: discord.Interaction) -> None:
        await self._change_page(interaction, -1)

    async def next_page(self, interaction: discord.Interaction) -> None:
        await self._change_page(interaction, 1)


class SpeedProtectBot(commands.Bot):
    def __init__(self, api_token: str) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        self.api_token = api_token
        self.api_session: aiohttp.ClientSession | None = None
        self.site_base_url = validate_site_base_url(SITE_BASE_URL)
        self._register_commands()

    async def _require_top_two_role(self, interaction: discord.Interaction) -> bool:
        if (
            interaction.guild is not None
            and isinstance(interaction.user, discord.Member)
            and member_role_rank(interaction.guild, interaction.user) in (1, 2)
        ):
            return True
        embed = discord.Embed(
            title="Permission denied",
            description="Your role does not have the required permissions. Please try again later.",
            color=discord.Color(0xE74C3C),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return False

    def _register_commands(self) -> None:
        @self.tree.command(name="setpanel", description="Post a script panel in this channel.")
        @app_commands.describe(name="Name shown at the top of the panel")
        async def setpanel(interaction: discord.Interaction, name: str) -> None:
            if not await self._require_top_two_role(interaction):
                return
            if interaction.guild_id is None or not isinstance(
                interaction.channel, (discord.TextChannel, discord.Thread),
            ):
                await interaction.response.send_message(
                    "Run `/setpanel` in a server text channel or thread.", ephemeral=True,
                )
                return
            if not name.strip() or len(name) > 80:
                await interaction.response.send_message(
                    "Use a non-empty panel name of up to 80 characters.", ephemeral=True,
                )
                return
            channel = interaction.channel
            await interaction.response.defer(ephemeral=True, thinking=True)
            total = await self.count_owned_loaders(interaction.user.id)
            if total == 0:
                await interaction.edit_original_response(
                    content="You have no scripts uploaded to the site yet. Upload one, then run `/setpanel` again."
                )
                return
            if total == 1:
                rows = await self.get_owned_loader_page(interaction.user.id, 0, limit=1)
                if not rows:
                    await interaction.edit_original_response(content="Your script is no longer available.")
                    return
                await self.publish_panel(
                    channel=channel,
                    guild_id=interaction.guild_id,
                    owner_id=interaction.user.id,
                    loader=rows[0],
                    panel_name=name,
                )
                await interaction.edit_original_response(content=f"Panel posted in <#{channel.id}>.")
                return
            rows = await self.get_owned_loader_page(interaction.user.id, 0)
            view = ScriptSelectionView(
                bot=self, channel=channel, guild_id=interaction.guild_id,
                owner_id=interaction.user.id, panel_name=name, total=total, rows=rows,
            )
            await interaction.edit_original_response(content="Choose a script for your panel:", view=view)

        @self.tree.command(name="planskey", description="Create a Premium or Ultra website plan key.")
        @app_commands.describe(plans="Which website plan this key unlocks")
        @app_commands.choices(plans=[
            app_commands.Choice(name="Premium", value="premium"),
            app_commands.Choice(name="Ultra", value="ultra"),
        ])
        async def planskey(interaction: discord.Interaction, plans: app_commands.Choice[str]) -> None:
            if not await self._require_top_two_role(interaction):
                return
            if interaction.user.id != PLANSKEY_ADMIN_ID:
                await interaction.response.send_message(
                    embed=discord.Embed(
                        title="Permission denied",
                        description="Only the bot administrator can create plan keys.",
                        color=discord.Color(0xE74C3C),
                    ),
                    ephemeral=True,
                )
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            result = await self.create_plan_key(plans.value)
            await interaction.edit_original_response(
                embed=discord.Embed(
                    title="Plan Key Created",
                    description=f"**Plan:** {plans.name}\n**Key:** `{result['licenseKey']}`",
                    color=discord.Color(0x2ECC71),
                )
            )

        @self.tree.command(name="resethwid", description="Reset all active HWIDs for a selected user.")
        @app_commands.describe(user="Discord member whose active key HWIDs will be reset")
        async def resethwid(interaction: discord.Interaction, user: discord.Member) -> None:
            if not await self._require_top_two_role(interaction):
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            result = await self.admin_reset_user_hwid(user.id)
            reset_count = result["resetCount"]
            desc = (
                f"No active keys were found for {user.mention}."
                if reset_count == 0
                else f"Reset HWID for {user.mention} across {reset_count} active key(s)."
            )
            await interaction.edit_original_response(
                embed=discord.Embed(title="HWID Reset", description=desc, color=discord.Color(0x2ECC71)),
                allowed_mentions=discord.AllowedMentions.none(),
            )

        @self.tree.command(name="genkey", description="Generate redeem keys for one of your scripts.")
        @app_commands.describe(
            days="Duration: 1m, 1s, 1d, 1y; use 0 for a lifetime key",
            amount="How many keys to create (optional; defaults to 1)",
        )
        async def genkey(interaction: discord.Interaction, days: str, amount: int | None = None) -> None:
            if not await self._require_top_two_role(interaction):
                return
            if interaction.guild_id is None or not isinstance(
                interaction.channel, (discord.TextChannel, discord.Thread),
            ):
                await interaction.response.send_message("Run `/genkey` in a server text channel or thread.", ephemeral=True)
                return
            duration_seconds = parse_duration(days)
            key_amount = amount if amount is not None else 1
            if not 1 <= key_amount <= MAX_KEY_AMOUNT:
                await interaction.response.send_message(f"Amount must be between 1 and {MAX_KEY_AMOUNT}.", ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            total = await self.count_owned_loaders(interaction.user.id)
            if total == 0:
                await interaction.edit_original_response(content="You have no scripts uploaded yet.")
                return
            if total == 1:
                rows = await self.get_owned_loader_page(interaction.user.id, 0, limit=1)
                keys = await self.create_keys(interaction.user.id, rows[0]["id"], duration_seconds, key_amount)
                await self.send_generated_keys(
                    interaction, keys=keys, loader=rows[0],
                    duration_label="lifetime" if duration_seconds == 0 else days.strip(),
                )
                return
            rows = await self.get_owned_loader_page(interaction.user.id, 0)
            view = ScriptSelectionView(
                bot=self, channel=interaction.channel, guild_id=interaction.guild_id,
                owner_id=interaction.user.id, panel_name="", total=total, rows=rows,
                key_duration_seconds=duration_seconds,
                duration_label="lifetime" if duration_seconds == 0 else days.strip(),
                key_amount=key_amount,
            )
            await interaction.edit_original_response(content="Choose a script for your keys:", view=view)

        @self.tree.command(name="cooldownhwid", description="Set the HWID reset cooldown for a script.")
        @app_commands.describe(time="Cooldown: 1m, 1s, 1d, 1y; use 0 to disable it")
        async def cooldownhwid(interaction: discord.Interaction, time: str) -> None:
            if not await self._require_top_two_role(interaction):
                return
            if interaction.guild_id is None or not isinstance(
                interaction.channel, (discord.TextChannel, discord.Thread),
            ):
                await interaction.response.send_message("Run `/cooldownhwid` in a server text channel or thread.", ephemeral=True)
                return
            duration_seconds = parse_duration(time)
            label = "lifetime" if duration_seconds == 0 else time.strip()
            await interaction.response.defer(ephemeral=True, thinking=True)
            total = await self.count_owned_loaders(interaction.user.id)
            if total == 0:
                await interaction.edit_original_response(content="You have no scripts uploaded yet.")
                return
            if total == 1:
                rows = await self.get_owned_loader_page(interaction.user.id, 0, limit=1)
                await self.set_hwid_cooldown(interaction.user.id, rows[0]["id"], duration_seconds)
                await interaction.edit_original_response(content=f"HWID cooldown for **{rows[0]['title']}** set to **{label}**.")
                return
            rows = await self.get_owned_loader_page(interaction.user.id, 0)
            view = ScriptSelectionView(
                bot=self, channel=interaction.channel, guild_id=interaction.guild_id,
                owner_id=interaction.user.id, panel_name="", total=total, rows=rows,
                duration_label=label, cooldown_duration_seconds=duration_seconds,
            )
            await interaction.edit_original_response(content="Choose a script for the HWID cooldown:", view=view)

        @self.tree.command(name="freemode", description="Enable or disable keyless access for one of your scripts.")
        @app_commands.describe(mode="Choose whether to enable or disable Free Mode")
        @app_commands.choices(mode=[
            app_commands.Choice(name="Enable Free Mode", value="enable"),
            app_commands.Choice(name="Disable Free Mode", value="disable"),
        ])
        async def freemode(interaction: discord.Interaction, mode: app_commands.Choice[str]) -> None:
            if not await self._require_top_two_role(interaction):
                return
            if interaction.guild_id is None or not isinstance(
                interaction.channel, (discord.TextChannel, discord.Thread),
            ):
                await interaction.response.send_message("Run `/freemode` in a server text channel or thread.", ephemeral=True)
                return
            enabled = mode.value == "enable"
            await interaction.response.defer(ephemeral=True, thinking=True)
            total = await self.count_owned_loaders(interaction.user.id)
            if total == 0:
                await interaction.edit_original_response(content="You have no scripts uploaded yet.")
                return
            if total == 1:
                rows = await self.get_owned_loader_page(interaction.user.id, 0, limit=1)
                result = await self.set_free_mode(interaction.user.id, rows[0]["id"], enabled)
                await interaction.edit_original_response(content=None, embed=free_mode_embed(result), view=None)
                return
            rows = await self.get_owned_loader_page(interaction.user.id, 0)
            view = ScriptSelectionView(
                bot=self, channel=interaction.channel, guild_id=interaction.guild_id,
                owner_id=interaction.user.id, panel_name="", total=total, rows=rows, free_mode_enabled=enabled,
            )
            await interaction.edit_original_response(content="Choose a script:", view=view)

        @self.tree.command(name="whitelist", description="Whitelist a user or every member of a role.")
        @app_commands.describe(time="Key duration: 1m, 1s, 1d, 1y; use 0 for lifetime", user="Discord user", role="Discord role")
        async def whitelist(
            interaction: discord.Interaction, time: str,
            user: discord.Member | None = None, role: discord.Role | None = None,
        ) -> None:
            if not await self._require_top_two_role(interaction):
                return
            duration_seconds = parse_duration(time)
            await self.handle_membership_command(interaction, "whitelist", user, role, duration_seconds)

        @self.tree.command(name="unwhitelist", description="Remove a user or every member of a role from a script.")
        @app_commands.describe(user="Discord user", role="Discord role")
        async def unwhitelist(
            interaction: discord.Interaction, user: discord.Member | None = None, role: discord.Role | None = None,
        ) -> None:
            if not await self._require_top_two_role(interaction):
                return
            await self.handle_membership_command(interaction, "unwhitelist", user, role, None)

        @self.tree.command(name="blacklist", description="Blacklist a user from a script.")
        @app_commands.describe(user="Discord user to blacklist")
        async def blacklist(interaction: discord.Interaction, user: discord.Member) -> None:
            if not await self._require_top_two_role(interaction):
                return
            await self.handle_membership_command(interaction, "blacklist", user, None, None)

        @self.tree.command(name="unblacklist", description="Remove a user's blacklist from a script.")
        @app_commands.describe(user="Discord user to unblock")
        async def unblacklist(interaction: discord.Interaction, user: discord.Member) -> None:
            if not await self._require_top_two_role(interaction):
                return
            await self.handle_membership_command(interaction, "unblacklist", user, None, None)

    async def resolve_membership_targets(
        self, interaction: discord.Interaction, user: discord.Member | None, role: discord.Role | None,
    ) -> list[discord.Member]:
        if interaction.guild is None:
            raise BotApiError(400, "Run this command inside a Discord server.")
        if (user is None) == (role is None):
            raise BotApiError(400, "Choose exactly one of `user` or `role`.")
        if user is not None:
            return [user]
        members = list(role.members)
        if not members:
            members = [
                member async for member in interaction.guild.fetch_members(limit=None)
                if any(member_role.id == role.id for member_role in member.roles)
            ]
        if not members:
            raise BotApiError(400, "That role has no members I can whitelist.")
        return members

    async def handle_membership_command(
        self, interaction: discord.Interaction, action: str,
        user: discord.Member | None, role: discord.Role | None, duration_seconds: int | None,
    ) -> None:
        if interaction.guild_id is None or not isinstance(
            interaction.channel, (discord.TextChannel, discord.Thread),
        ):
            await interaction.response.send_message(f"Run `/{action}` in a server text channel or thread.", ephemeral=True)
            return
        members = await self.resolve_membership_targets(interaction, user, role)
        await interaction.response.defer(ephemeral=True, thinking=True)
        total = await self.count_owned_loaders(interaction.user.id)
        if total == 0:
            await interaction.edit_original_response(content="You have no scripts uploaded yet.")
            return
        if total == 1:
            rows = await self.get_owned_loader_page(interaction.user.id, 0, limit=1)
            loader = rows[0]
            if action == "whitelist":
                result = await self.whitelist_users(
                    interaction.user.id, loader["id"], [str(m.id) for m in members], duration_seconds
                )
                panel = await self.get_panel_for_script(interaction.guild_id, interaction.user.id, loader["id"])
                await self.post_whitelist_message(
                    interaction, interaction.channel, role.mention if role else user.mention,
                    members, result, loader["title"], self.panel_message_url(interaction.guild_id, panel),
                )
                await interaction.edit_original_response(content=f"Whitelisted {len(result['keys'])} user(s).")
            elif action == "unwhitelist":
                result = await self.unwhitelist_users(interaction.user.id, loader["id"], [str(m.id) for m in members])
                await interaction.edit_original_response(content=f"Unwhitelisted ({result['removed']} key(s) revoked).")
            elif action == "blacklist":
                await self.blacklist_users(interaction.user.id, loader["id"], [str(m.id) for m in members])
                await interaction.edit_original_response(content=f"Blacklisted {len(members)} user(s).")
            else:
                await self.unblacklist_users(interaction.user.id, loader["id"], [str(m.id) for m in members])
                await interaction.edit_original_response(content=f"Unblacklisted {len(members)} user(s).")
            return
        rows = await self.get_owned_loader_page(interaction.user.id, 0)
        view = ScriptSelectionView(
            bot=self, channel=interaction.channel, guild_id=interaction.guild_id,
            owner_id=interaction.user.id, panel_name="", total=total, rows=rows,
            membership_action=action, membership_members=members,
            membership_duration_seconds=duration_seconds,
            membership_target_mention=role.mention if role else user.mention,
        )
        await interaction.edit_original_response(content=f"Choose a script to {action}:", view=view)

    async def post_whitelist_message(
        self, interaction: discord.Interaction, channel: discord.abc.Messageable,
        target_mention: str | None, members: list[discord.Member],
        result: dict, title: str, panel_url: str | None,
    ) -> None:
        if any(not item.get("existing") for item in result.get("keys", [])):
            await channel.send(
                f"{target_mention or '@user'}\n"
                f"You have been whitelisted for **{title}**!\n"
                f"You can access the script via this message --> {panel_url or channel.mention}"
            )

    async def setup_hook(self) -> None:
        timeout = aiohttp.ClientTimeout(total=15)
        self.api_session = aiohttp.ClientSession(
            timeout=timeout,
            headers={"Authorization": f"Bot {self.api_token}"},
        )
        await self._api_request("GET", "/api/bot/health")
        self.add_view(PanelButtons(self))
        await self.tree.sync()

    async def close(self) -> None:
        if self.api_session is not None and not self.api_session.closed:
            await self.api_session.close()
        await super().close()

    async def _api_request(
        self, method: str, path: str, *, params: dict | None = None, json_body: dict | None = None,
    ) -> dict:
        if self.api_session is None or self.api_session.closed:
            raise RuntimeError("The HTTPS bot API session is not ready")
        url = f"{self.site_base_url}{path}"
        async with self.api_session.request(method, url, params=params, json=json_body) as response:
            try:
                payload = await response.json(content_type=None)
            except (aiohttp.ContentTypeError, ValueError):
                payload = {}
            if response.status >= 400:
                message = payload.get("message") if isinstance(payload, dict) else None
                code = payload.get("error") if isinstance(payload, dict) else None
                raise BotApiError(response.status, message or "The site API request failed.", code)
            return payload if isinstance(payload, dict) else {}

    async def count_owned_loaders(self, owner_id: int) -> int:
        payload = await self._api_request("GET", "/api/bot/scripts", params={"ownerId": str(owner_id), "page": "0", "limit": "1"})
        return int(payload.get("total", 0))

    async def get_owned_loader_page(self, owner_id: int, page: int, limit: int = PAGE_SIZE) -> list[dict]:
        payload = await self._api_request(
            "GET", "/api/bot/scripts",
            params={"ownerId": str(owner_id), "page": str(max(0, page)), "limit": str(max(1, min(limit, PAGE_SIZE)))},
        )
        scripts = payload.get("scripts", [])
        return scripts if isinstance(scripts, list) else []

    async def get_owned_loader(self, owner_id: int, loader_id: str) -> dict | None:
        if not str(loader_id).isdigit():
            return None
        try:
            return await self._api_request("GET", f"/api/bot/scripts/{loader_id}", params={"ownerId": str(owner_id)})
        except BotApiError as error:
            if error.status == 404:
                return None
            raise

    async def get_panel_for_script(self, guild_id: int, owner_id: int, loader_id: int | str) -> dict | None:
        payload = await self._api_request(
            "GET", "/api/bot/panel-for-script",
            params={"guildId": str(guild_id), "ownerId": str(owner_id), "loaderId": str(loader_id)},
        )
        panel = payload.get("panel")
        return panel if isinstance(panel, dict) else None

    @staticmethod
    def panel_message_url(guild_id: int, panel: dict | None) -> str | None:
        if not panel or not panel.get("channelId") or not panel.get("messageId"):
            return None
        return f"https://discord.com/channels/{guild_id}/{panel['channelId']}/{panel['messageId']}"

    async def save_panel(
        self, guild_id: int, channel_id: int, message_id: int,
        owner_id: int, loader_id: int | str, panel_name: str, panel_description: str,
    ) -> None:
        await self._api_request(
            "POST", "/api/bot/panels",
            json_body={
                "guildId": str(guild_id), "channelId": str(channel_id), "messageId": str(message_id),
                "ownerId": str(owner_id), "loaderId": str(loader_id),
                "panelName": panel_name, "panelDescription": panel_description,
            },
        )

    async def create_keys(self, owner_id: int, loader_id: int | str, duration_seconds: int, amount: int) -> list[str]:
        payload = await self._api_request(
            "POST", "/api/bot/keys",
            json_body={"ownerId": str(owner_id), "loaderId": str(loader_id), "durationSeconds": duration_seconds, "amount": amount},
        )
        return payload.get("keys") or []

    async def set_hwid_cooldown(self, owner_id: int, loader_id: int | str, duration_seconds: int) -> dict:
        return await self._api_request(
            "POST", "/api/bot/cooldown-hwid",
            json_body={"ownerId": str(owner_id), "loaderId": str(loader_id), "durationSeconds": duration_seconds},
        )

    async def set_free_mode(self, owner_id: int, loader_id: int | str, enabled: bool) -> dict:
        return await self._api_request(
            "POST", "/api/bot/free-mode",
            json_body={"ownerId": str(owner_id), "loaderId": str(loader_id), "enabled": enabled},
        )

    async def create_plan_key(self, plan: str) -> dict:
        return await self._api_request("POST", "/api/bot/plan-keys", json_body={"plan": plan})

    async def whitelist_users(self, owner_id: int, loader_id: int | str, user_ids: list[str], duration_seconds: int | None) -> dict:
        return await self._api_request(
            "POST", "/api/bot/whitelist",
            json_body={"ownerId": str(owner_id), "loaderId": str(loader_id), "userIds": user_ids, "durationSeconds": duration_seconds},
        )

    async def unwhitelist_users(self, owner_id: int, loader_id: int | str, user_ids: list[str]) -> dict:
        return await self._api_request(
            "POST", "/api/bot/unwhitelist",
            json_body={"ownerId": str(owner_id), "loaderId": str(loader_id), "userIds": user_ids},
        )

    async def check_blacklist(self, guild_id: int, channel_id: int, message_id: int, user_id: int) -> bool:
        payload = await self._api_request(
            "POST", "/api/bot/blacklist-check",
            json_body={"guildId": str(guild_id), "channelId": str(channel_id), "messageId": str(message_id), "discordUserId": str(user_id)},
        )
        return bool(payload.get("blacklisted"))

    async def blacklist_users(self, owner_id: int, loader_id: int | str, user_ids: list[str]) -> dict:
        return await self._api_request(
            "POST", "/api/bot/blacklist",
            json_body={"ownerId": str(owner_id), "loaderId": str(loader_id), "userIds": user_ids},
        )

    async def unblacklist_users(self, owner_id: int, loader_id: int | str, user_ids: list[str]) -> dict:
        return await self._api_request(
            "POST", "/api/bot/unblacklist",
            json_body={"ownerId": str(owner_id), "loaderId": str(loader_id), "userIds": user_ids},
        )

    async def redeem_key(self, guild_id: int, channel_id: int, message_id: int, user_id: int, raw_key: str) -> dict:
        return await self._api_request(
            "POST", "/api/bot/redeem",
            json_body={"guildId": str(guild_id), "channelId": str(channel_id), "messageId": str(message_id), "discordUserId": str(user_id), "key": raw_key},
        )

    async def get_script_access(self, guild_id: int, channel_id: int, message_id: int, user_id: int) -> dict:
        return await self._api_request(
            "POST", "/api/bot/script-access",
            json_body={"guildId": str(guild_id), "channelId": str(channel_id), "messageId": str(message_id), "discordUserId": str(user_id)},
        )

    async def get_key_stats(self, guild_id: int, channel_id: int, message_id: int, user_id: int) -> dict:
        return await self._api_request(
            "POST", "/api/bot/stats",
            json_body={"guildId": str(guild_id), "channelId": str(channel_id), "messageId": str(message_id), "discordUserId": str(user_id)},
        )

    async def reset_hwid(self, guild_id: int, channel_id: int, message_id: int, user_id: int) -> dict:
        return await self._api_request(
            "POST", "/api/bot/reset-hwid",
            json_body={"guildId": str(guild_id), "channelId": str(channel_id), "messageId": str(message_id), "discordUserId": str(user_id)},
        )

    async def admin_reset_user_hwid(self, user_id: int) -> dict:
        return await self._api_request(
            "POST", "/api/bot/admin-reset-hwid",
            json_body={"discordUserId": str(user_id)},
        )

    async def send_generated_keys(
        self, interaction: discord.Interaction, keys: list[str], loader: dict, duration_label: str | None,
    ) -> None:
        header = f"Created {len(keys)} key(s) for **{loader.get('title', 'Script')}** · `{duration_label or '—'}`."
        await interaction.edit_original_response(content=f"{header}\n```text\n" + "\n".join(keys) + "\n```", view=None)

    async def publish_panel(
        self, channel: PanelChannel, guild_id: int, owner_id: int, loader: dict, panel_name: str,
    ) -> discord.Message:
        embed = panel_embed(panel_name, loader["title"])
        view = PanelButtons(self)
        message = await channel.send(embed=embed, view=view)
        await self.save_panel(
            guild_id=guild_id, channel_id=channel.id, message_id=message.id,
            owner_id=owner_id, loader_id=loader["id"],
            panel_name=panel_name, panel_description=embed.description or "",
        )
        return message


async def run_bot() -> None:
    bot_token, api_token = await resolve_tokens_from_site(SITE_BASE_URL)
    if not bot_token:
        raise SystemExit(
            "Bot token tapılmadı. Sayt API-sindən gəlməyibsə DISCORD_BOT_TOKEN mühit dəyişənini təyin edin."
        )
    bot = SpeedProtectBot(api_token)
    async with bot:
        await bot.start(bot_token)


if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        pass
