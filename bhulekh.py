"""Shared session handling for the Odisha Bhulekh RoR portal.

The portal is a single ASP.NET WebForms page driven entirely by postbacks:
every dropdown fires ``__doPostBack`` and the server returns the whole page
with the next dropdown filled in. There is no JSON API and no stable URL for
a record, so a scrape has to replay the cascade
district -> tahsil -> RI -> village and carry ``__VIEWSTATE`` forward at each
step.

Two facts drive the callers:

* ``rbtnRORSearchtype=Tenant`` fills ``ddlBindData`` with every tenant in the
  village in one response, which is how names are enumerated cheaply.
* ``rbtnRORSearchtype=Khatiyan`` plus ``btnRORFront`` renders one RoR, which
  is the only place the caste appears.

A direct GET of RoRView.aspx redirects to an error page, so ``Session.open``
fetches the root first to be issued a session cookie.
"""

from __future__ import annotations

import gzip
import http.client
import http.cookiejar
import logging
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from html.parser import HTMLParser

import certifi

ROOT = "https://bhulekh.ori.nic.in/"
ROR_VIEW = ROOT + "RoRView.aspx"
PREFIX = "ctl00$ContentPlaceHolder1$"

DISTRICT = PREFIX + "ddlDistrict"
TAHSIL = PREFIX + "ddlTahsil"
RI = PREFIX + "ddlRI"
VILLAGE = PREFIX + "ddlVillage"
BIND = PREFIX + "ddlBindData"
SEARCH_TYPE = PREFIX + "rbtnRORSearchtype"
VIEW_ROR = PREFIX + "btnRORFront"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# The portal publishes its own totals on the landing page. They are the only
# external check on whether an enumeration is complete, so they are asserted
# rather than printed.
EXPECTED = {
    "districts": 30,
    "tahsils": 317,
    "ri_circles": 2721,
    "villages": 51796,
    "khatiyans": 20432717,
    "tenants": 47166788,
}

RETRYABLE = (
    urllib.error.URLError,
    http.client.RemoteDisconnected,
    http.client.IncompleteRead,
    ConnectionError,
    TimeoutError,
    ssl.SSLError,
)

THROTTLE_STATUS = 429
THROTTLE_BACKOFF = (60, 180, 420)
PERMANENT_STATUS = frozenset({400, 403, 404, 410, 451})

CONTEXT = ssl.create_default_context(cafile=certifi.where())

logger = logging.getLogger("bhulekh")


class PortalError(RuntimeError):
    """The portal answered, but not with the page we asked for."""


class FormParser(HTMLParser):
    """Collect hidden inputs and dropdown options from one rendered page.

    Attributes:
        hidden: ``name -> value`` for every hidden input.
        selects: ``name -> [(value, label)]`` for every ``select``.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hidden: dict[str, str] = {}
        self.selects: dict[str, list[tuple[str, str]]] = {}
        self._select: str | None = None
        self._value: str | None = None
        self._label: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Record hidden inputs, and open a select or option.

        Args:
            tag: The element name.
            attrs: Its attributes.
        """
        got = {key: (value or "") for key, value in attrs}
        if tag == "input" and got.get("type") == "hidden" and got.get("name"):
            self.hidden[got["name"]] = got.get("value", "")
        elif tag == "select" and got.get("name"):
            self._select = got["name"]
            self.selects.setdefault(self._select, [])
        elif tag == "option" and self._select is not None:
            self._value = got.get("value", "")
            self._label = []

    def handle_data(self, data: str) -> None:
        """Accumulate the text of the option being read.

        Args:
            data: A run of character data.
        """
        if self._value is not None:
            self._label.append(data)

    def handle_endtag(self, tag: str) -> None:
        """Close the current option or select.

        Args:
            tag: The element name.
        """
        if tag == "option" and self._select is not None and self._value is not None:
            label = "".join(self._label).strip()
            self.selects[self._select].append((self._value, label))
            self._value, self._label = None, []
        elif tag == "select":
            self._select = None


@dataclass
class Session:
    """A Bhulekh browsing session that carries ``__VIEWSTATE`` forward.

    Attributes:
        pause: Seconds to wait after each request.
        timeout: Seconds before a single attempt is abandoned.
        attempts: Attempts per request before giving up.
    """

    pause: float = 0.8
    timeout: int = 45
    attempts: int = 3
    _opener: urllib.request.OpenerDirector = field(init=False)
    _hidden: dict[str, str] = field(init=False, default_factory=dict)
    _selects: dict[str, list[tuple[str, str]]] = field(init=False, default_factory=dict)

    def __post_init__(self) -> None:
        jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar),
            urllib.request.HTTPSHandler(context=CONTEXT),
        )
        self._opener.addheaders = [
            ("User-Agent", USER_AGENT),
            ("Accept-Encoding", "gzip"),
            ("Referer", ROR_VIEW),
        ]

    def open(self) -> None:
        """Start a session at the landing page and absorb its form state."""
        self._absorb(self._fetch(ROOT, None))

    def _fetch(self, url: str, data: bytes | None) -> str:
        last: Exception | None = None
        for attempt in range(1, self.attempts + 1):
            try:
                with self._opener.open(url, data, timeout=self.timeout) as response:
                    body = response.read()
                    if response.headers.get("Content-Encoding") == "gzip":
                        body = gzip.decompress(body)
                time.sleep(self.pause)
                return body.decode("utf8", "replace")
            except urllib.error.HTTPError as error:
                last = error
                # A rate limit is not a transport failure and must not be
                # retried on the same short backoff. Handling this only after
                # seeing one cost the Karnataka fetch 1,889 documents, which
                # were recorded as permanently absent when they were merely
                # throttled.
                if error.code == THROTTLE_STATUS:
                    retry_after = error.headers.get("Retry-After") if error.headers else None
                    wait = THROTTLE_BACKOFF[min(attempt - 1, len(THROTTLE_BACKOFF) - 1)]
                    if retry_after and str(retry_after).isdigit():
                        wait = max(wait, int(retry_after))
                    logger.warning("throttled by the portal; waiting %ds", wait)
                    if attempt < self.attempts:
                        time.sleep(wait)
                    continue
                if error.code in PERMANENT_STATUS:
                    raise PortalError(f"{url} returned {error.code}") from error
                if attempt < self.attempts:
                    time.sleep(4 * attempt)
            except RETRYABLE as error:
                last = error
                if attempt < self.attempts:
                    time.sleep(4 * attempt)
        raise PortalError(f"{url} failed after {self.attempts} attempts: {last}")

    def _absorb(self, body: str) -> None:
        parser = FormParser()
        parser.feed(body)
        if "__VIEWSTATE" not in parser.hidden:
            raise PortalError("response carried no __VIEWSTATE")
        self._hidden, self._selects = parser.hidden, parser.selects

    def _payload(self, fields: dict[str, str]) -> bytes:
        data = dict(self._hidden)
        # These two submit buttons render on the browser-compatibility notice.
        # Posting them navigates away from the form.
        for name in ("btnProceed", "btnie"):
            data.pop(PREFIX + name, None)
        data.update(fields)
        return urllib.parse.urlencode(data, encoding="utf8").encode("utf8")

    def postback(self, target: str, fields: dict[str, str]) -> None:
        """Fire one ``__doPostBack`` and absorb the resulting page.

        Args:
            target: The control name that raised the event.
            fields: Currently selected dropdown values to send along.
        """
        payload = dict(fields)
        payload["__EVENTTARGET"] = target
        payload["__EVENTARGUMENT"] = ""
        self._absorb(self._fetch(ROR_VIEW, self._payload(payload)))

    def submit(self, fields: dict[str, str], button: str, label: str) -> str:
        """Press a submit button and return the raw HTML, without absorbing it.

        The RoR page carries no dropdowns, so absorbing it would discard the
        form state the caller needs for the next record.

        Args:
            fields: Currently selected dropdown values.
            button: Control name of the button to press.
            label: Value to send for that button.

        Returns:
            The response body.
        """
        payload = dict(fields)
        payload["__EVENTTARGET"] = ""
        payload["__EVENTARGUMENT"] = ""
        payload[button] = label
        return self._fetch(ROR_VIEW, self._payload(payload))

    def options(self, name: str) -> list[tuple[str, str]]:
        """Return ``(value, label)`` for one dropdown, minus its prompt row.

        Args:
            name: The ``name`` attribute of the ``select`` element.

        Returns:
            Option pairs, with placeholder rows such as "Select District"
            and blank values removed.
        """
        return [
            (value, label)
            for value, label in self._selects.get(name, [])
            if value and not value.startswith("Select") and label
        ]
