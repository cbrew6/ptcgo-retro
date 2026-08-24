"""
Local stand-in for the PTCGO asset CDN (versionURL / assetURL).

The client's preloader aborts with "couldn't load preload data" when it
cannot reach the CDN, which happens before the login screen is usable.
It needs two files (pie-bundles.dll, LoadManifestVersion / LoadManifest):

  {versionURL}bundles/pc/manifest.version           -> plain integer
  {assetURL}bundles/pc/manifest_{N}.manifest        -> GZIPped JSON of
                                                       AssetBundleManifestV3

AssetBundleManifestV3 fields (Unity JsonUtility, so names are exact):
    platform          string
    bundles           AssetBundleDescriptorV3[]
    preloadBundles    string[]
    forceloadBundles  string[]
    version           int

Serving an empty bundle list is enough to get through preload: the client
finds nothing to download and continues to login. Card art and other bundled
content come from the 233 .unity3d files already shipped in StreamingAssets;
wiring those into `bundles` is the next step once login is confirmed.

Every unmatched request is logged with its path so the remaining CDN
dependencies can be discovered from real client traffic.
"""

import gzip
import json
import logging
import os
import re
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8081
# Bump when the manifest contents change - the client caches the manifest by
# version number and won't re-fetch otherwise.
# 8: asset names now come from the real CDN manifest (donor/manifest.json)
#    fully qualified, instead of being reconstructed from bundle-name prefixes.
# 6: bundle_index.json is now read from each bundle's own m_Container instead
#    of by walking strings, which removed 1,279 invented asset names (and
#    added one real one). Invented names matter: DoesAssetExistInManifest is
#    what makes the client commit to a request, so a name no bundle can serve
#    sends it down a branch with no fallback.
MANIFEST_VERSION = 8

log = logging.getLogger("assets")

LOCALE = "en_US"
PLATFORM = "pc"

INDEX_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "bundle_index.json")


def load_index():
    """bundle name -> asset names, produced by bundle_index.py."""
    if not os.path.exists(INDEX_PATH):
        log.error("%s missing - run bundle_index.py, or every image will be "
                  "black", INDEX_PATH)
        return {}
    with open(INDEX_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def asset_aliases(bundle, names):
    """Register the asset names this bundle answers for.

    A name that already contains "/" came from the real CDN manifest and is
    exactly what the original server declared, so it is registered verbatim -
    no prefix is derived and none is added. Everything below applies only to
    bare leaf names recovered by extraction, where the namespace has to be
    reconstructed.

    Register "{prefix}/{asset}" for each underscore-delimited prefix.

    Foil bundles must NOT claim the bare set prefix. Card art is requested as
    "{set}/{number}" and foil masks as "{set}_{mask}/{number}", but a bundle
    like XY12_wp_ph_Foil_CR85 holds asset "011" just as XY12_fire_CR85 does.
    Allowing the foil bundle to also claim "XY12/011" makes it overwrite the
    art in the asset map (last writer wins), so the client asks for the card
    face and gets handed a foil mask - foils render, art doesn't.

    So for any bundle carrying a wp_<mask> segment, aliases start at the
    prefix that includes that segment and never get shorter.
    """
    qualified = [n for n in names if "/" in n]
    if qualified:
        return [{"name": n} for n in qualified]

    parts = bundle.split("_")
    start = 1
    for k, p in enumerate(parts):
        if p == "wp" and k + 1 < len(parts):
            start = k + 2          # keep e.g. "XY12_wp_ph", never plain "XY12"
            break
    prefixes = {"_".join(parts[:i]) for i in range(start, len(parts) + 1)}
    out = []
    for pref in sorted(prefixes):
        for n in names:
            out.append({"name": "%s/%s" % (pref, n)})
    return out


BUNDLE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "PokemonTradingCardGameOnline",
    "Pokemon Trading Card Game Online_Data",
    "StreamingAssets",
    LOCALE,
)


def discover_bundles():
    """Build AssetBundleDescriptorV3 entries for the shipped .unity3d files.

    Filenames are {locale}_{name}_{version}.unity3d, and the client rebuilds
    that exact path in getPrecachePath():

        file://{streamingAssetsPath}/{alt_locale}/{alt_locale}_{name}_{alt_version}.unity3d

    so as long as name/version round-trip, the client loads straight off disk
    and never touches the network.

    `assets` is populated from bundle_index.json and is NOT optional: every
    art request in the client is gated behind
    DoesAssetExistInManifest(assetName), which is just a lookup in the
    asset-name -> bundle map built from these arrays. Leave it empty and the
    client never requests a single bundle - everything renders black.

    Lookups use the name FormatAssetRequest() builds, "{bundle}/{asset}",
    where {bundle} is a logical name that may be shorter than the real bundle
    name: card art asks for "XY12/064" while the bundle is
    "XY12_colorless_CR85", and cosmetics ask for "deckBoxes/x" while the
    bundle is "deckBoxes_CR72". Since the logical name isn't recoverable from
    the file, register every underscore-delimited prefix as an alias. They all
    point at the same descriptor, so extra aliases are harmless.
    """
    if not os.path.isdir(BUNDLE_DIR):
        log.error("no bundle directory at %s - images will be missing",
                  BUNDLE_DIR)
        return []

    index = load_index()
    bundles = []
    total_assets = 0
    for fn in sorted(os.listdir(BUNDLE_DIR)):
        if not fn.endswith(".unity3d"):
            continue
        stem = fn[: -len(".unity3d")]
        if not stem.startswith(LOCALE + "_"):
            continue
        stem = stem[len(LOCALE) + 1:]
        name, _, ver = stem.rpartition("_")
        if not name or not ver.isdigit():
            continue
        version = int(ver)
        assets = asset_aliases(name, index.get(name, []))
        total_assets += len(assets)
        bundles.append({
            "name": name,
            "assets": assets,
            # SingleOrDefault is used to pick these by locale, so exactly one
            # entry per locale - a duplicate would throw.
            "versionings": [{
                "platform": PLATFORM,
                "locale": LOCALE,
                "version": version,
                "alt_version": -1,
                "CRC": 0,
            }],
            "precached": [{
                "alt_locale": LOCALE,
                "alt_platform": PLATFORM,
                "alt_version": version,
            }],
            "timesensitive": 0,
        })
    log.info("discovered %d local asset bundles, %d asset entries",
             len(bundles), total_assets)
    return bundles


MANIFEST = {
    "platform": PLATFORM,
    "bundles": discover_bundles(),
    "preloadBundles": [],
    "forceloadBundles": [],
    "version": MANIFEST_VERSION,
}

# The patcher is disabled via ShouldPatch=false in cake.cfg, but serve a
# well-formed manifest anyway so the check is harmless if it ever runs.
# Fields map to U.e: A/a = windows/mac refresher URL, B/b = client versions.
PATCH_MANIFEST = {
    "A": "", "a": "",
    "B": 2095, "b": 2095,
}

# Dynamic client config (pie-src class E.o). Values mirror the class's own
# field defaults; Registration=false hides the "create account" path, which
# would point at Pokemon's dead SSO. Analytics/collector are left blank so the
# client does not try to phone home.
CLIENT_CONFIG = {
    "Registration": False,
    "iospatchversion": 0,
    "androidpatchversion": 0,
    "unsupportedDeviceAction": "",
    "ChildLogTime": 2.0,
    "helpButtonDestination": "",
    "bacgroundRelease": "",
    "collectorURL": "",
    "localizedSignup": "logindialog.helpLink.url.signup",
    # These two MUST be parseable URIs. The client feeds them straight to
    # WebRequest.Create, so blanking them throws UriFormatException and stalls
    # the loading bar at 0%. The shipped defaults point at Pokemon's dead SSO;
    # aim them at this server instead so nothing leaves the machine.
    #
    # hostName has no "/sso" suffix on purpose: the CAS command builds
    #     hostName + "/sso/login?service=" + serviceID + "&locale=" + lang
    # so putting it here too produced /sso/sso/login and a 404, which the
    # client reports as "couldn't reach pokemon.com".
    "serviceID": "http://127.0.0.1:8081/sso/game_client_signin",
    "hostName": "http://127.0.0.1:8081",
    "disableAnalytics": "true",
}

# --------------------------------------------------------------------------
# CAS single sign-on stand-in
# --------------------------------------------------------------------------
#
# Logging in with a username instead of a device ID is what makes the client
# treat the account as a real one rather than a guest, and that is what stops
# the account upsell, the "Have Fun!" dialog and the forced walk into Trainer
# Challenge. That path goes through Pokemon's CAS server, which is gone.
#
# The client does an ordinary two-step CAS scrape (pie-src, command o.X):
#
#   GET  {hostName}/sso/login?service={serviceID}&locale={lang}
#        -> parses two hidden fields out of the HTML:
#             <input type="hidden" name="lt" value="([^"]+)
#             <input type="hidden" name="execution" value="([^"]+)
#   POST same URL with lt, execution, _eventId=submit, username, password
#        -> looks for a ticket, first in a "Location" response header and
#           then in the body, both with the regex \?ticket=([^&]+)
#
# So a redirect to {service}?ticket=ST-... is all it needs. Any username and
# password are accepted: the WARG side auto-creates accounts anyway, and this
# is a local server with no one else on it.

SSO_LOGIN_PATH = "/sso/login"

SSO_FORM = """<!doctype html>
<html><head><title>Sign in</title></head><body>
<form method="post" action="{action}">
<input type="hidden" name="lt" value="{lt}" />
<input type="hidden" name="execution" value="{execution}" />
<input type="hidden" name="_eventId" value="submit" />
<input type="text" name="username" />
<input type="password" name="password" />
<input type="submit" value="Sign in" />
</form>
</body></html>
"""

VERSION_RE = re.compile(r"^/bundles/(?:[^/]+/)*manifest\.version$")
MANIFEST_RE = re.compile(r"^/bundles/(?:[^/]+/)*manifest_(\d+)\.manifest$")
BUNDLE_RE = re.compile(r"^/bundles/(?:[^/]+/)*([^/]+\.unity3d)$")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quieten the default stderr spam
        pass

    def _send(self, body, content_type="application/octet-stream", status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _service_from_query(self):
        qs = self.path.split("?", 1)[1] if "?" in self.path else ""
        params = urllib.parse.parse_qs(qs)
        return (params.get("service") or [""])[0]

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path != SSO_LOGIN_PATH:
            log.warning("404 POST %s", path)
            self._send(b"not found", "text/plain", status=404)
            return

        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        fields = urllib.parse.parse_qs(body.decode("utf-8", "replace"))
        username = (fields.get("username") or [""])[0]

        service = self._service_from_query()
        ticket = "ST-" + uuid.uuid4().hex
        target = "%s%sticket=%s" % (service, "&" if "?" in service else "?",
                                    ticket)
        log.info("-> SSO ticket for %r: %s", username, ticket)
        # ScrapeHeader checks the Location header first; a 302 is the shape a
        # real CAS server replies with. The body carries the ticket too, so
        # ScrapeTicket still finds it if the redirect is followed instead.
        body = ("<html><body>Redirecting to %s</body></html>" % target).encode()
        self.send_response(302)
        self.send_header("Location", target)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]

        if path == SSO_LOGIN_PATH:
            page = SSO_FORM.format(action=self.path,
                                   lt="LT-" + uuid.uuid4().hex,
                                   execution="e1s1").encode("utf-8")
            log.info("-> sso/login form")
            self._send(page, "text/html")
            return

        if VERSION_RE.match(path):
            log.info("-> manifest.version = %d  (%s)", MANIFEST_VERSION, path)
            self._send(str(MANIFEST_VERSION).encode(), "text/plain")
            return

        m = MANIFEST_RE.match(path)
        if m:
            body = gzip.compress(json.dumps(MANIFEST).encode("utf-8"))
            log.info("-> manifest_%s.manifest (%d bundles, %d bytes gzip)",
                     m.group(1), len(MANIFEST["bundles"]), len(body))
            self._send(body)
            return

        # Actual bundle downloads. getWebPath() builds
        #   {assetURL}bundles/pc/{locale}/{locale}_{name}_{version}.unity3d
        # Serving these from StreamingAssets means the client can fetch any
        # bundle over HTTP even when the precached file:// path doesn't
        # resolve. Drop additional .unity3d files into BUNDLE_DIR (e.g. real
        # card art recovered from elsewhere) and they become available here.
        m = BUNDLE_RE.match(path)
        if m:
            name = os.path.basename(m.group(1))       # no traversal
            local = os.path.join(BUNDLE_DIR, name)
            if os.path.isfile(local):
                with open(local, "rb") as fh:
                    body = fh.read()
                log.info("-> bundle %s (%d bytes)", name, len(body))
                self._send(body)
            else:
                log.warning("bundle not on disk: %s", name)
                self._send(b"not found", "text/plain", status=404)
            return

        if path == "/clientconfig/config.json":
            log.info("-> clientconfig/config.json")
            self._send(json.dumps(CLIENT_CONFIG).encode(), "application/json")
            return

        if path == "/motd/motd.json":
            log.info("-> motd/motd.json (empty)")
            self._send(json.dumps({"messages": []}).encode(), "application/json")
            return

        if path == "/patch/manifest.json":
            log.info("-> patch/manifest.json")
            self._send(json.dumps(PATCH_MANIFEST).encode(), "application/json")
            return

        log.warning("404 %s", path)
        self._send(b"not found", "text/plain", status=404)

    def do_HEAD(self):
        self.do_GET()


def serve():
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    log.info("asset server listening on http://127.0.0.1:%d", PORT)
    httpd.serve_forever()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s [assets] %(message)s",
                        datefmt="%H:%M:%S")
    serve()
