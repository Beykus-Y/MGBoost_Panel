// PH7-16 Wave 0B — application shell entry point.
// The single `<script type="module">` tag in index.html. Composition
// root: dynamically imports kernel.js (side effect only -- wires the
// modal-overlay dismiss-on-backdrop-click handler) and auth.js, then
// dynamically imports the legacy admin.js module and registers its
// exported `bootstrap` as auth.js's post-authentication callback, before
// finally kicking off the same session-restore flow admin.js's own
// trailing `restoreAdminSession();` call used to trigger directly.
//
// admin.js's internal structure is untouched by this wave -- it is still
// the single legacy monolith holding every screen not yet split into a
// canonical per-domain module (that split is Wave 4/5, out of scope here).
//
// Single source of truth for cache-busting: the `?v=` query string on
// *this* file's own <script src> in index.html. `import.meta.url` reads
// it back once here, and every dynamic import() this shell (and admin.js,
// and auth.js internally) performs propagates the exact same string --
// see the comments in admin.js and auth.js for why that propagation is
// load-bearing, not cosmetic, given the server's real
// `Cache-Control: public, max-age=3600` on JS assets.
const MODULE_VERSION = new URL(import.meta.url).search;

await import(`./kernel.js${MODULE_VERSION}`);
const {onAuthenticated, restoreAdminSession} = await import(`./auth.js${MODULE_VERSION}`);
const {bootstrap} = await import(`../../admin.js${MODULE_VERSION}`);

onAuthenticated(bootstrap);
restoreAdminSession();
