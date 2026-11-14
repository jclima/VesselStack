/*
 * Node-RED settings seeded by the VesselStack installer (README "Node-RED (optional)").
 *
 * Installed root-owned at <VESSELSTACK_ROOT>/config/node-red/settings.js and mounted read-only at
 * /vesselstack/settings.js; Node-RED is started with --settings pointing there, so code running inside
 * the container cannot rewrite the sign-in, and a missing file stops Node-RED instead of starting it
 * with unauthenticated defaults. The file is created ONLY when it does not exist; the installer never
 * overwrites it (edit it as root). The sign-in hashes and the credential secret are not stored here:
 * the installer passes them to the container in config/nodered.env, and this file refuses to start
 * Node-RED without them. They are removed from process.env after being read (flows and the Exec node
 * cannot read them), but remain visible to anyone who can run "docker inspect".
 * The same crew sign-in is also required on the Dashboard 2.0 live channel (dashboard.ioMiddleware below).
 */
const hash = process.env.NODERED_ADMIN_PASSWORD_HASH
const dashboardHash = process.env.NODERED_DASHBOARD_PASSWORD_HASH
const credentialSecret = process.env.NODERED_CREDENTIAL_SECRET
if (!/^\$2[aby]\$/.test(hash || '') || !/^\$2[aby]\$/.test(dashboardHash || '') || !credentialSecret) {
    throw new Error('VesselStack: a Node-RED password hash or NODERED_CREDENTIAL_SECRET is missing; rerun the VesselStack installer')
}
delete process.env.NODERED_ADMIN_PASSWORD_HASH
delete process.env.NODERED_DASHBOARD_PASSWORD_HASH
delete process.env.NODERED_CREDENTIAL_SECRET

// Dashboard 2.0 serves its live channel (/dashboard/socket.io/) straight from the HTTP server, so httpNodeAuth
// never sees it. dashboard.ioMiddleware runs on every socket.io connection and requires the same crew sign-in
// (the browser resends the Basic credentials it cached for the dashboard page). Setting it replaces the
// dashboard's built-in check, which is kept: no direct websocket connections, no cross-origin pages.
const crypto = require('crypto')
let bcrypt // resolved lazily from Node-RED's own install (@node-rs/bcrypt, else bcryptjs); null = none found
function loadBcrypt () {
    if (bcrypt !== undefined) return bcrypt
    bcrypt = null
    const { createRequire } = require('module')
    const bases = [require.main && require.main.filename, '/usr/src/node-red/node_modules/node-red/red.js'].filter(Boolean)
    for (const base of bases) {
        for (const name of ['@node-rs/bcrypt', 'bcryptjs']) {
            try { bcrypt = createRequire(base)(name); return bcrypt } catch (e) { /* try the next one */ }
        }
    }
    console.error('VesselStack: no bcrypt module found; every dashboard live connection is refused')
    return bcrypt
}
let crewDigest = null // SHA-256 of the last "crew:<passphrase>" that matched (the hash cannot change without a restart)
const crewChecks = new Map() // in-flight bcrypt checks by digest, so a burst of reconnects costs one bcrypt
function crewSignedIn (authorization) {
    const match = /^Basic +([A-Za-z0-9+/]+=*) *$/i.exec(authorization || '')
    if (!match) return Promise.resolve(false)
    const decoded = Buffer.from(match[1], 'base64').toString('utf8')
    const colon = decoded.indexOf(':')
    if (colon < 0 || decoded.slice(0, colon) !== 'crew') return Promise.resolve(false)
    const digest = crypto.createHash('sha256').update(decoded).digest()
    if (crewDigest && crypto.timingSafeEqual(digest, crewDigest)) return Promise.resolve(true)
    const key = digest.toString('hex')
    if (crewChecks.has(key)) return crewChecks.get(key)
    const lib = loadBcrypt()
    if (!lib || crewChecks.size >= 8) return Promise.resolve(false) // fail closed; bound concurrent bcrypt work
    const check = Promise.resolve()
        .then(() => lib.compare(decoded.slice(colon + 1), dashboardHash))
        .then(ok => { if (ok === true) crewDigest = digest; return ok === true }, () => false)
        .finally(() => crewChecks.delete(key))
    crewChecks.set(key, check)
    return check
}
function dashboardSocketAuth (socket, next) {
    const request = socket.request || {}
    if (/[?&]transport=websocket(&|$)/.test(request.url || '') || socket.handshake.xdomain !== false) {
        return next(new Error('VesselStack: refused'))
    }
    crewSignedIn((request.headers || {}).authorization)
        .then(ok => next(ok ? undefined : new Error('VesselStack: crew sign-in required')),
            () => next(new Error('VesselStack: crew sign-in required')))
}

module.exports = {
    flowFile: 'flows.json',
    credentialSecret: credentialSecret,
    uiPort: process.env.PORT || 1880,

    // Editor sign-in (user admin) and dashboard sign-in (user crew) are different passphrases.
    adminAuth: {
        type: 'credentials',
        users: [{ username: 'admin', password: hash, permissions: '*' }]
    },
    httpNodeAuth: { user: 'crew', pass: dashboardHash },
    // The same crew sign-in on the Dashboard 2.0 live channel (see dashboardSocketAuth above).
    dashboard: { ioMiddleware: dashboardSocketAuth },

    // No telemetry, update checks, diagnostics endpoint or Projects (git) feature.
    telemetry: { enabled: false, updateNotification: false },
    diagnostics: { enabled: false, ui: false },
    runtimeState: { enabled: false, ui: false },
    editorTheme: { projects: { enabled: false } },

    // Function nodes cannot pull in arbitrary npm modules by name.
    functionExternalModules: false,
    // Installing nodes from the editor's palette manager stays on (it runs npm as the container user,
    // and a node can run any code): leave it only on a network you trust. Set allowInstall: false to lock it.
    externalModules: { palette: { allowInstall: true } },

    logging: { console: { level: 'info', metrics: false, audit: false } },
    exportGlobalContextKeys: false
}
