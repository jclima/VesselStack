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
