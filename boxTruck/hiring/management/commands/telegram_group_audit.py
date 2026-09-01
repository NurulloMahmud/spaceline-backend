"""
Audit the Telegram groups the Spaceline bots sit in.

Written to answer two questions that came out of the "the bot is removing
drivers from their groups" report:

  1. Which bots are administrators in a driver's group, and does any of them
     actually hold `can_restrict_members`? A bot without that right cannot
     remove anyone, whatever a service message appears to say.

  2. Is a specific person currently banned from a group? `getChatMember`
     reports `kicked` for a standing ban and `left` for someone who is simply
     not in the chat.

It also mints an invite link on request, because the Bot API has no way to add
a member: `addChatMember` does not exist for bots, only for user accounts on
MTProto. A link the person clicks is the only path in that a bot can offer.

Deliberately never calls `getUpdates`. agent_bot long-polls these same tokens
in production, and a second consumer on one token steals its updates -- driver
load offers would go missing while this ran.

Group ids come from `Driver.telegram_group_id`, since the Bot API has no
method to enumerate the chats a bot belongs to.

    ./venv/bin/python manage.py telegram_group_audit
    ./venv/bin/python manage.py telegram_group_audit --title "A-0002"
    ./venv/bin/python manage.py telegram_group_audit --title "A-0002" --check-user 8623386887
    ./venv/bin/python manage.py telegram_group_audit --title "A-0002" --invite
"""
from pathlib import Path

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from hiring.models import Driver

API_ROOT = "https://api.telegram.org"
TIMEOUT = 15


def _call(token, method, **params):
    """One Bot API call. Returns (ok, result_or_description)."""
    try:
        resp = requests.post(
            f"{API_ROOT}/bot{token}/{method}", data=params, timeout=TIMEOUT
        )
    except requests.RequestException as e:
        return False, f"request failed: {e}"

    try:
        body = resp.json()
    except ValueError:
        return False, f"HTTP {resp.status_code}, unparseable body"

    if not body.get("ok"):
        return False, body.get("description", f"HTTP {resp.status_code}")
    return True, body.get("result")


def _agent_bot_token(repo_root):
    """agent_bot keeps its token in its own .env, not in Django settings."""
    env_path = repo_root / "agent_bot" / ".env"
    if not env_path.is_file():
        return None
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line.startswith("TELEGRAM_BOT_TOKEN="):
            return line.split("=", 1)[1].strip() or None
    return None


def _redact(token):
    """A token identifies a bot by the digits before the colon; the secret
    after it never needs to appear in output that lands in a terminal log."""
    return token.split(":", 1)[0] + ":<redacted>"


class Command(BaseCommand):
    help = (
        "Report which bots administer each driver Telegram group and whether "
        "any of them can remove members. Read-only unless --invite is passed."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--title",
            help=(
                "Only groups whose Telegram title contains this text "
                "(case-insensitive). Without it, every linked group is audited."
            ),
        )
        parser.add_argument(
            "--token",
            action="append",
            default=[],
            dest="tokens",
            help=(
                "Bot token to audit with, repeatable. Defaults to Django's "
                "BOT_TOKEN plus agent_bot/.env's TELEGRAM_BOT_TOKEN."
            ),
        )
        parser.add_argument(
            "--check-user",
            help="Telegram user id to look up in each matched group.",
        )
        parser.add_argument(
            "--invite",
            action="store_true",
            help=(
                "Create a single-use invite link for each matched group. This "
                "is the only write this command performs."
            ),
        )

    def handle(self, *args, **options):
        tokens = self._resolve_tokens(options["tokens"])
        bots = self._identify(tokens)
        if not bots:
            raise CommandError("No usable bot token — nothing to audit.")

        groups = self._group_ids()
        if not groups:
            raise CommandError("No driver has a telegram_group_id set.")

        self.stdout.write(
            f"\n{len(groups)} linked group(s) on file, "
            f"{len(bots)} bot token(s) in play.\n"
        )

        matched = 0
        for chat_id, drivers in sorted(groups.items()):
            title, seen_by = self._resolve_title(bots, chat_id)
            if title is None:
                continue
            if options["title"] and options["title"].lower() not in title.lower():
                continue

            matched += 1
            self._report(chat_id, title, drivers, seen_by, bots, options)

        if not matched:
            self.stdout.write(
                self.style.WARNING(
                    "\nNo group matched. Either no bot is a member of it, or "
                    "its id is not on any driver record."
                )
            )

    # --- setup ----------------------------------------------------------

    def _resolve_tokens(self, explicit):
        if explicit:
            return list(dict.fromkeys(explicit))

        tokens = []
        django_token = getattr(settings, "BOT_TOKEN", None)
        if django_token:
            tokens.append(django_token)

        # settings.BASE_DIR is boxTruck/; the sibling services live beside it.
        agent_token = _agent_bot_token(Path(settings.BASE_DIR).parent)
        if agent_token:
            tokens.append(agent_token)

        return list(dict.fromkeys(tokens))

    def _identify(self, tokens):
        """Map each working token to the bot it belongs to."""
        bots = []
        for token in tokens:
            ok, result = _call(token, "getMe")
            if not ok:
                self.stdout.write(
                    self.style.ERROR(f"  {_redact(token)} — getMe failed: {result}")
                )
                continue
            bots.append(
                {
                    "token": token,
                    "id": result["id"],
                    "username": result.get("username") or result.get("first_name"),
                }
            )
            self.stdout.write(
                self.style.SUCCESS(
                    f"  {_redact(token)} — @{bots[-1]['username']} (id {result['id']})"
                )
            )
        return bots

    def _group_ids(self):
        """Distinct group ids, each with the drivers pointing at it. A group
        carrying more than one driver is itself worth knowing about."""
        groups = {}
        rows = (
            Driver.objects
            .exclude(telegram_group_id__isnull=True)
            .exclude(telegram_group_id__exact="")
            .values_list("telegram_group_id", "full_name")
        )
        for chat_id, name in rows:
            groups.setdefault(chat_id.strip(), []).append(name)
        return groups

    # --- per-group ------------------------------------------------------

    def _resolve_title(self, bots, chat_id):
        """The chat's title, via whichever bots can actually see the chat."""
        title = None
        seen_by = []
        for bot in bots:
            ok, result = _call(bot["token"], "getChat", chat_id=chat_id)
            if ok:
                seen_by.append(bot)
                title = title or result.get("title") or "(no title)"
        return title, seen_by

    def _report(self, chat_id, title, drivers, seen_by, bots, options):
        self.stdout.write("\n" + "=" * 72)
        self.stdout.write(self.style.MIGRATE_HEADING(f"{title}"))
        self.stdout.write(f"  chat_id : {chat_id}")
        self.stdout.write(f"  drivers : {', '.join(drivers)}")

        blind = [b for b in bots if b not in seen_by]
        if blind:
            self.stdout.write(
                "  not a member: "
                + ", ".join(f"@{b['username']}" for b in blind)
            )

        for bot in seen_by:
            self._report_admins(bot, chat_id)
            if options["check_user"]:
                self._report_member(bot, chat_id, options["check_user"])
            if options["invite"]:
                self._make_invite(bot, chat_id)

    def _report_admins(self, bot, chat_id):
        ok, admins = _call(bot["token"], "getChatAdministrators", chat_id=chat_id)
        if not ok:
            self.stdout.write(
                self.style.ERROR(f"  [@{bot['username']}] admins: {admins}")
            )
            return

        self.stdout.write(f"  [@{bot['username']}] administrators:")
        for admin in admins:
            user = admin.get("user", {})
            who = user.get("username") or user.get("first_name") or user.get("id")
            kind = "BOT" if user.get("is_bot") else "user"
            can_restrict = admin.get("can_restrict_members")
            flag = ""
            if user.get("is_bot") and can_restrict:
                # The whole point of the audit: a bot holding this right is a
                # bot that could have removed someone.
                flag = self.style.ERROR("  <-- CAN REMOVE MEMBERS")
            elif user.get("is_bot"):
                flag = self.style.SUCCESS("  <-- cannot remove members")
            self.stdout.write(
                f"      {kind:4} @{who} "
                f"status={admin.get('status')} "
                f"can_restrict_members={can_restrict}{flag}"
            )

    def _report_member(self, bot, chat_id, user_id):
        ok, member = _call(
            bot["token"], "getChatMember", chat_id=chat_id, user_id=user_id
        )
        if not ok:
            self.stdout.write(
                self.style.ERROR(f"  [@{bot['username']}] user {user_id}: {member}")
            )
            return

        status = member.get("status")
        note = {
            "kicked": "BANNED — a removal that is still in force",
            "left": "not in the chat, and not banned",
            "member": "in the chat",
            "administrator": "in the chat, as an admin",
            "creator": "the chat owner",
            "restricted": "in the chat, restricted",
        }.get(status, "")
        self.stdout.write(
            f"  [@{bot['username']}] user {user_id}: status={status}  {note}"
        )
        if status == "kicked" and member.get("until_date"):
            self.stdout.write(f"      ban until_date={member['until_date']}")

    def _make_invite(self, bot, chat_id):
        ok, link = _call(
            bot["token"],
            "createChatInviteLink",
            chat_id=chat_id,
            name="audit access",
            member_limit=1,
        )
        if not ok:
            self.stdout.write(
                self.style.ERROR(f"  [@{bot['username']}] invite link: {link}")
            )
            return
        self.stdout.write(
            self.style.SUCCESS(
                f"  [@{bot['username']}] single-use invite: {link['invite_link']}"
            )
        )
