"""WhatsApp Fix - Handles Baileys registered=false bug"""

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

CREDS_FILE = Path.home() / ".openclaw/credentials/whatsapp/default/creds.json"

def fix_registered_flag() -> bool:
    """Fix Baileys registered=false bug. Returns True if fix applied."""
    logger.info(f"[WhatsApp Monitor] Starting fix_registered_flag check...")
    logger.info(f"[WhatsApp Monitor] Checking credentials file: {CREDS_FILE}")

    if not CREDS_FILE.exists():
        logger.info(f"[WhatsApp Monitor] Credentials file does not exist - no WhatsApp linked yet")
        return False

    try:
        logger.info(f"[WhatsApp Monitor] Reading credentials file...")
        with open(CREDS_FILE, 'r') as f:
            creds = json.load(f)

        has_account = bool(creds.get("account"))
        has_me = bool(creds.get("me", {}).get("id"))
        registered = creds.get("registered", False)

        logger.info(f"[WhatsApp Monitor] Credential state: has_account={has_account}, has_me={has_me}, registered={registered}")

        if has_account and has_me:
            phone_id = creds.get("me", {}).get("id", "unknown")
            logger.info(f"[WhatsApp Monitor] WhatsApp account found: {phone_id}")

            if not registered:
                logger.info(f"[WhatsApp Monitor] DETECTED registered=false bug! Fixing...")
                creds["registered"] = True
                with open(CREDS_FILE, 'w') as f:
                    json.dump(creds, f)
                logger.info(f"[WhatsApp Monitor] SUCCESS: Fixed registered=false for {phone_id}")
                return True
            else:
                logger.info(f"[WhatsApp Monitor] registered=true already set, no fix needed")
        else:
            logger.info(f"[WhatsApp Monitor] Incomplete credentials - has_account={has_account}, has_me={has_me}")

    except Exception as e:
        logger.error(f"[WhatsApp Monitor] ERROR reading/fixing credentials: {e}")

    return False

def get_whatsapp_status() -> dict:
    """Get basic WhatsApp status."""
    logger.info(f"[WhatsApp Monitor] Getting WhatsApp status...")

    if not CREDS_FILE.exists():
        logger.info(f"[WhatsApp Monitor] No credentials file - WhatsApp not linked")
        return {"linked": False, "phone": None, "registered": False}

    try:
        with open(CREDS_FILE, 'r') as f:
            creds = json.load(f)

        jid = creds.get("me", {}).get("id", "")
        phone = "+" + jid.split(":")[0] if ":" in jid else None
        linked = bool(creds.get("account"))
        registered = creds.get("registered", False)

        status = {
            "linked": linked,
            "phone": phone,
            "registered": registered
        }
        logger.info(f"[WhatsApp Monitor] Status: {status}")
        return status
    except Exception as e:
        logger.error(f"[WhatsApp Monitor] ERROR getting status: {e}")
        return {"linked": False, "phone": None, "registered": False}
