"""ServerChan protocol constants shared by configuration and delivery."""

SERVERCHAN_TITLE_MAX_CHARACTERS = 32
SERVERCHAN_TURBO_BASE_URL = "https://sctapi.ftqq.com"
SERVERCHAN_TURBO_SENDKEY_PATTERN = r"SCT[A-Za-z0-9]+"
SERVERCHAN_3_PUSH_DOMAIN = "push.ft07.com"
SERVERCHAN_3_SENDKEY_PATTERN = r"sctp(?P<uid>[0-9]+)t[A-Za-z0-9]+"
