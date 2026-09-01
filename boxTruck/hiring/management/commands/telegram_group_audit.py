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

    ./venv/bin/python manage.py telegram_group_audit --identity
    ./venv/bin/python manage.py telegram_group_audit
    ./venv/bin/python manage.py telegram_group_audit --driver "Edgar Cortes"
    ./venv/bin/python manage.py telegram_group_audit --scan-logs --title "Edgar Cortes"
    ./venv/bin/python manage.py telegram_group_audit --chat-id -1001234567890
    ./venv/bin/python manage.py telegram_group_audit --chat-id -1001234567890 --check-user 8623386887
    ./venv/bin/python manage.py telegram_group_audit --chat-id -1001234567890 --invite
"""
import glob
import re
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
            "--identity",
            action="store_true",
            help=(
                "Resolve every token this deployment configures to the bot it "
                "actually belongs to, and stop. Answers whether Spaceline "
                "really runs two Telegram bots or one."
            ),
        )
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
            "--chat-id",
            help=(
                "Audit this Telegram chat id directly, even if no driver record "
                "carries it. Use when the group was never linked in the TMS -- "
                "open the group in Telegram Web and the id is the -100... number "
                "in the URL, or forward one of its messages to @userinfobot."
            ),
        )
        parser.add_argument(
            "--scan-logs",
            action="store_true",
            help=(
                "Recover chat ids from agent_bot's own logs and audit each. The "
                "Bot API cannot list a bot's groups, but agent_bot logs the chat "
                "id of every message it sees, so a group the bot is in but that "
                "was never linked in the TMS is discoverable this way. Combine "
                "with --title to home in on one group by name."
            ),
        )
        parser.add_argument(
            "--log-file",
            action="append",
            default=[],
            dest="log_files",
            help=(
                "A log file for --scan-logs to read, repeatable. Defaults to the "
                "usual supervisor paths for agent_bot if none is given."
            ),
        )
        parser.add_argument(
            "--driver",
            help=(
                "Before auditing, print every Driver whose name matches this "
                "text and the telegram_group_id on record for them. Answers "
                "'is this driver's group even linked?'."
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
        if options["identity"]:
            self._identity_report()
            return

        tokens = self._resolve_tokens(options["tokens"])
        bots = self._identify(tokens)
        if not bots:
            raise CommandError("No usable bot token — nothing to audit.")

        if options["driver"]:
            self._driver_lookup(options["driver"])

        groups = self._group_ids()

        # A raw chat id lets us probe a group that no driver record points at
        # -- e.g. one the customer named by title but that was never linked.
        if options["chat_id"]:
            groups.setdefault(options["chat_id"].strip(), []).append("(from --chat-id)")

        # Chat ids harvested from agent_bot's logs -- the practical way to find
        # a group the bot is in that the TMS never linked.
        if options["scan_logs"]:
            for chat_id in self._scan_logs(options["log_files"]):
                groups.setdefault(chat_id, []).append("(from logs)")

        if not groups:
            raise CommandError(
                "Nothing to audit: no driver has a telegram_group_id, no "
                "--chat-id was given, and --scan-logs found no chat ids."
            )

        self.stdout.write(
            f"\n{len(groups)} group id(s) to audit, "
            f"{len(bots)} bot token(s) in play.\n"
        )

        reported = 0
        for chat_id, drivers in sorted(groups.items()):
            title, seen_by, reasons = self._resolve_title(bots, chat_id)

            # Filter on title only when a bot could actually read one. A group
            # no bot can see is never silently dropped: not seeing it is itself
            # the finding -- the bot may have been removed from that very chat.
            if options["title"] and title and options["title"].lower() not in title.lower():
                continue

            reported += 1
            self._report(chat_id, title, drivers, seen_by, reasons, bots, options)

        if reported == 0:
            self.stdout.write(
                self.style.WARNING("\nNo group on file to report.")
            )

        # The Bot API cannot list the chats a bot belongs to, so a group the
        # customer names but that no driver row carries is invisible here. Say
        # so plainly rather than letting silence imply "clean".
        if options["title"] and not any(
            options["title"].lower() in " ".join(d).lower() for d in groups.values()
        ):
            self.stdout.write(
                self.style.WARNING(
                    f"\nNote: only {len(groups)} group id(s) are stored on driver "
                    f"records. If the group titled '{options['title']}' is not among "
                    "the titles above, it is simply not linked to any driver in the "
                    "TMS -- the Bot API cannot look a group up by title, only by a "
                    "chat id we already hold. Get its chat id (see --help) and pass "
                    "--chat-id to audit it directly."
                )
            )

    # --- identity -------------------------------------------------------

    def _identity_report(self):
        """Which bot does each configured token actually belong to?

        Three call sites -- billing/bot.py and billing/utils.py, all of them
        driver-facing -- read `settings.TELEGRAM_WEB_APP_TOKEN`, which the
        committed config/settings.py never assigns. Either this box runs an
        uncommitted settings.py that does assign it, or those three functions
        raise AttributeError and no message has ever left them. This says
        which, and if the setting does exist, whether it is a second bot or
        the same one under another name.
        """
        repo_root = Path(settings.BASE_DIR).parent
        sources = [
            ("BOT_TOKEN (django settings)", getattr(settings, "BOT_TOKEN", None)),
            (
                "TELEGRAM_WEB_APP_TOKEN (django settings)",
                getattr(settings, "TELEGRAM_WEB_APP_TOKEN", None),
            ),
            ("agent_bot/.env TELEGRAM_BOT_TOKEN", _agent_bot_token(repo_root)),
            (
                "email-agent/.env TELEGRAM_BOT_TOKEN",
                self._env_token(repo_root / "email-agent" / ".env"),
            ),
        ]

        self.stdout.write("\nConfigured Telegram tokens\n" + "=" * 72)
        identities = {}
        for label, token in sources:
            if not token:
                # An absent setting is the finding, not an error to skip past.
                self.stdout.write(
                    self.style.WARNING(f"  {label}\n      NOT SET / attribute missing")
                )
                continue

            ok, result = _call(token, "getMe")
            if not ok:
                self.stdout.write(
                    self.style.ERROR(f"  {label}\n      {_redact(token)} — getMe failed: {result}")
                )
                continue

            self.stdout.write(
                f"  {label}\n"
                f"      {_redact(token)} -> @{result.get('username')} (bot id {result['id']})"
            )
            identities.setdefault(result["id"], []).append(label)

        self.stdout.write("\n" + "=" * 72)
        if not identities:
            self.stdout.write(
                self.style.ERROR("No token resolved. Nothing is posting to Telegram.")
            )
        elif len(identities) == 1:
            bot_id, labels = next(iter(identities.items()))
            self.stdout.write(
                self.style.WARNING(
                    f"ONE bot (id {bot_id}) behind {len(labels)} setting name(s). "
                    "The names differ; the bot does not."
                )
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(f"{len(identities)} genuinely distinct bots:")
            )
            for bot_id, labels in identities.items():
                self.stdout.write(f"    bot id {bot_id}: {', '.join(labels)}")

        if getattr(settings, "TELEGRAM_WEB_APP_TOKEN", None) is None:
            self.stdout.write(
                self.style.ERROR(
                    "\nsettings.TELEGRAM_WEB_APP_TOKEN does not exist on this box.\n"
                    "  billing/bot.py:94, billing/utils.py:235 and :264 read it, so all\n"
                    "  three raise AttributeError and send nothing. Statement notices and\n"
                    "  load notices to driver groups are silently dead."
                )
            )

    @staticmethod
    def _env_token(env_path):
        if not env_path.is_file():
            return None
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line.startswith("TELEGRAM_BOT_TOKEN="):
                return line.split("=", 1)[1].strip() or None
        return None

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

    # Telegram group/supergroup ids are negative; supergroups are -100 + digits.
    # Match those as whole tokens so a phone number in a log line is not read
    # as a chat id.
    _CHAT_ID_RE = re.compile(r"(?<![\d-])(-\d{6,})(?![\d])")

    DEFAULT_LOG_GLOBS = [
        "/var/log/agent_bot.err.log*",
        "/var/log/agent_bot.out.log*",
        "/var/log/supervisor/agent_bot*.log*",
    ]

    def _scan_logs(self, log_files):
        paths = []
        if log_files:
            for pattern in log_files:
                paths.extend(sorted(glob.glob(pattern)) or [pattern])
        else:
            for pattern in self.DEFAULT_LOG_GLOBS:
                paths.extend(sorted(glob.glob(pattern)))

        self.stdout.write("\nScanning agent_bot logs for chat ids\n" + "-" * 72)
        found = {}
        for path in paths:
            p = Path(path)
            if not p.is_file():
                self.stdout.write(self.style.WARNING(f"  {path} — not found"))
                continue
            try:
                text = p.read_text(errors="replace")
            except OSError as e:
                self.stdout.write(self.style.ERROR(f"  {path} — unreadable: {e}"))
                continue
            hits = set(self._CHAT_ID_RE.findall(text))
            for cid in hits:
                found.setdefault(cid, set()).add(p.name)
            self.stdout.write(f"  {path} — {len(hits)} distinct chat id(s)")

        if not found:
            self.stdout.write(
                self.style.WARNING(
                    "  No chat ids in the logs. Either the logs have rotated away "
                    "or the bot has logged no group traffic. Pass --log-file with "
                    "the right path, or fetch the id from Telegram directly."
                )
            )
        else:
            self.stdout.write(f"  -> {len(found)} distinct chat id(s) to resolve")
        return sorted(found)

    def _driver_lookup(self, needle):
        rows = (
            Driver.objects
            .filter(full_name__icontains=needle)
            .values_list("id", "full_name", "telegram_group_id")
        )
        self.stdout.write(f"\nDrivers matching '{needle}':\n" + "-" * 72)
        if not rows:
            self.stdout.write(self.style.WARNING("  none"))
            return
        for pk, name, gid in rows:
            state = gid if gid else self.style.WARNING("(no telegram_group_id set)")
            self.stdout.write(f"  #{pk}  {name}  ->  {state}")

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
        """The chat's title, via whichever bots can see the chat, plus the
        per-bot reason for any that can't. 'bot is not a member of the chat'
        vs 'chat not found' distinguishes a removed bot from a stale id, and
        both matter to the removal question."""
        title = None
        seen_by = []
        reasons = {}
        for bot in bots:
            ok, result = _call(bot["token"], "getChat", chat_id=chat_id)
            if ok:
                seen_by.append(bot)
                title = title or result.get("title") or "(no title)"
            else:
                reasons[bot["username"]] = result
        return title, seen_by, reasons

    def _report(self, chat_id, title, drivers, seen_by, reasons, bots, options):
        self.stdout.write("\n" + "=" * 72)
        self.stdout.write(
            self.style.MIGRATE_HEADING(title or "(no bot can see this chat)")
        )
        self.stdout.write(f"  chat_id : {chat_id}")
        self.stdout.write(f"  drivers : {', '.join(drivers)}")

        # Why a bot can't see the chat is a finding in itself. If the driver
        # bot reports it is no longer a member of a group it used to serve,
        # that is a bot that was removed -- not a bot that removed anyone.
        for bot in bots:
            if bot not in seen_by:
                why = reasons.get(bot["username"], "unknown")
                self.stdout.write(
                    self.style.WARNING(f"  [@{bot['username']}] cannot see chat: {why}")
                )

        if not seen_by:
            self.stdout.write(
                "  -> No configured bot is in this chat, so its admin list and "
                "member states cannot be read from here."
            )
            return

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
