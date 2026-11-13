/*
 * Node-RED settings seeded by the VesselStack installer (README "Node-RED (optional)").
 *
 * This file is created ONLY when it does not exist; the installer never overwrites it, so your
 * edits survive upgrades. The sign-in hash and the credential secret are not stored here: the
 * installer passes them to the container in config/nodered.env, and this file refuses to start
 * Node-RED without them, so an editor can never come up unauthenticated by accident.
 */
const hash = process.env.NODERED_ADMIN_PASSWORD_HASH
const credentialSecret = process.env.NODERED_CREDENTIAL_SECRET
if (!/^\$2[aby]\$/.test(hash || '') || !credentialSecret) {
    throw new Error('VesselStack: NODERED_ADMIN_PASSWORD_HASH or NODERED_CREDENTIAL_SECRET is missing; rerun the VesselStack installer')
}

module.exports = {
    flowFile: 'flows.json',
    credentialSecret: credentialSecret,
    uiPort: process.env.PORT || 1880,

    // The editor and the dashboard (httpNode) use the same sign-in. User: admin.
    adminAuth: {
        type: 'credentials',
        users: [{ username: 'admin', password: hash, permissions: '*' }]
    },
    httpNodeAuth: { user: 'admin', pass: hash },

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
