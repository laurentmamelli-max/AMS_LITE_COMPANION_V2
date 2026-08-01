import AppKit
import CoreGraphics
import Foundation
import WebKit

private let dashboardURL = URL(string: "http://127.0.0.1:8766/")!
private let embeddedDashboardURL = URL(string: "http://127.0.0.1:8766/?embedded=1")!
private let catalogURL = URL(string: "http://127.0.0.1:8766/?catalog=1")!
private let visionURL = URL(string: "http://127.0.0.1:8766/?vision=1")!
private let stateURL = URL(string: "http://127.0.0.1:8766/api/state")!
private let healthURL = URL(string: "http://127.0.0.1:8766/api/health")!
private let shutdownURL = URL(string: "http://127.0.0.1:8766/api/shutdown")!
private let confirmBridgeURL = URL(string: "http://127.0.0.1:8766/api/bridge/confirm")!
private let useSavedBridgeURL = URL(string: "http://127.0.0.1:8766/api/bridge/use-saved")!

final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler {
    private var statusItem: NSStatusItem!
    private var statusLine: NSMenuItem!
    private var panelMenuItem: NSMenuItem!
    private var dockMenuItem: NSMenuItem!
    private var spoolLines: [NSMenuItem] = []
    private var panel: NSPanel!
    private var webView: WKWebView!
    // NSWindowController owns the auxiliary window for its full lifetime.
    // Creating and showing a bare NSWindow from WebKit's script callback made
    // the catalogue window vulnerable to a re-entrant AppKit crash.
    private var catalogWindowController: NSWindowController?
    private var catalogWebView: WKWebView?
    private var visionWindowController: NSWindowController?
    private var visionWebView: WKWebView?
    private var engine: Process?
    private var engineLog: FileHandle?
    private var pollTimer: Timer?
    private var bambuSeen = false
    private var bambuMissingPolls = 0
    private var quitting = false
    private var panelDocked = true
    private var apiToken = ""
    private var mappingPromptKey: String?
    private var safetyPromptKey: String?

    func applicationDidFinishLaunching(_ notification: Notification) {
        UserDefaults.standard.register(defaults: ["panelDocked": true])
        apiToken = UserDefaults.standard.string(forKey: "apiToken") ?? ""
        if apiToken.isEmpty {
            apiToken = UUID().uuidString.replacingOccurrences(of: "-", with: "")
            UserDefaults.standard.set(apiToken, forKey: "apiToken")
        }
        panelDocked = UserDefaults.standard.bool(forKey: "panelDocked")
        buildMenu()
        buildPanel()
        startEngine(showPanel: true)
        launchBambuStudio()
        pollTimer = Timer.scheduledTimer(timeInterval: 3.0,
                                         target: self,
                                         selector: #selector(poll),
                                         userInfo: nil,
                                         repeats: true)
        poll()
    }

    func applicationWillTerminate(_ notification: Notification) {
        pollTimer?.invalidate()
        if let process = engine, process.isRunning {
            process.terminate()
        }
    }

    private func buildMenu() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let button = statusItem.button {
            if #available(macOS 11.0, *) {
                button.image = NSImage(systemSymbolName: "circle.grid.2x2.fill",
                                       accessibilityDescription: "AMS Lite Companion V2")
            } else {
                button.title = "AMS"
            }
            button.toolTip = "AMS Lite Companion V2"
        }

        let menu = NSMenu()
        let title = NSMenuItem(title: "AMS Lite Companion V2 · développement", action: nil, keyEquivalent: "")
        title.isEnabled = false
        menu.addItem(title)

        statusLine = NSMenuItem(title: "Démarrage…", action: nil, keyEquivalent: "")
        statusLine.isEnabled = false
        menu.addItem(statusLine)
        menu.addItem(.separator())

        for slot in 1...4 {
            let line = NSMenuItem(title: "A\(slot) · Chargement…", action: nil, keyEquivalent: "")
            line.isEnabled = false
            spoolLines.append(line)
            menu.addItem(line)
        }

        menu.addItem(.separator())
        panelMenuItem = NSMenuItem(title: "Afficher le panneau Companion",
                                   action: #selector(togglePanel),
                                   keyEquivalent: "p")
        menu.addItem(panelMenuItem)
        menu.addItem(NSMenuItem(title: "Ouvrir le catalogue de bobines",
                                action: #selector(showCatalog),
                                keyEquivalent: "c"))
        menu.addItem(NSMenuItem(title: "Ouvrir le centre Vision",
                                action: #selector(showVision),
                                keyEquivalent: "v"))
        dockMenuItem = NSMenuItem(title: "Suivre la fenêtre Bambu Studio",
                                  action: #selector(toggleDocking),
                                  keyEquivalent: "d")
        dockMenuItem.state = panelDocked ? .on : .off
        menu.addItem(dockMenuItem)
        menu.addItem(NSMenuItem(title: "Ouvrir le tableau complet dans le navigateur",
                                action: #selector(openBrowserDashboard),
                                keyEquivalent: "o"))
        menu.addItem(NSMenuItem(title: "Ouvrir Bambu Studio",
                                action: #selector(openBambu),
                                keyEquivalent: "b"))
        menu.addItem(NSMenuItem(title: "Redémarrer le moteur",
                                action: #selector(restartEngine),
                                keyEquivalent: "r"))
        menu.addItem(NSMenuItem(title: "Afficher le journal",
                                action: #selector(openLog),
                                keyEquivalent: "l"))
        menu.addItem(.separator())
        menu.addItem(NSMenuItem(title: "Quitter Companion",
                                action: #selector(quitCompanion),
                                keyEquivalent: "q"))
        menu.items.forEach { $0.target = self }
        statusItem.menu = menu
    }

    private func buildPanel() {
        let visible = NSScreen.main?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
        let width = min(440.0, max(390.0, visible.width * 0.3))
        let height = min(760.0, visible.height - 30.0)
        let rect = NSRect(x: visible.maxX - width,
                          y: visible.maxY - height,
                          width: width,
                          height: height)
        panel = NSPanel(contentRect: rect,
                        styleMask: [.titled, .closable, .resizable, .utilityWindow],
                        backing: .buffered,
                        defer: false)
        panel.title = "AMS Lite Companion V2"
        panel.minSize = NSSize(width: 370, height: 480)
        panel.isReleasedWhenClosed = false
        panel.hidesOnDeactivate = false
        panel.isFloatingPanel = false
        panel.collectionBehavior = [.fullScreenAuxiliary]
        panel.delegate = self

        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .default()
        configuration.userContentController.add(self, name: "companion")
        webView = WKWebView(frame: panel.contentView?.bounds ?? .zero, configuration: configuration)
        webView.autoresizingMask = [.width, .height]
        webView.navigationDelegate = self
        webView.uiDelegate = self
        panel.contentView = webView
    }

    @objc private func showCatalog() {
        // Leave WebKit's message-delivery stack before creating an AppKit
        // window. A user click reaches this method through WKScriptMessage.
        DispatchQueue.main.async { [weak self] in
            self?.presentCatalog()
        }
    }

    private func presentCatalog() {
        if let window = catalogWindowController?.window {
            window.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
            return
        }

        let rect = NSRect(x: 180, y: 160, width: 1180, height: 680)
        let window = NSWindow(contentRect: rect,
                              styleMask: [.titled, .closable, .miniaturizable, .resizable],
                              backing: .buffered,
                              defer: false)
        window.title = "Catalogue de bobines"
        window.minSize = NSSize(width: 820, height: 460)
        window.delegate = self

        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .default()
        configuration.userContentController.add(self, name: "companion")
        let view = WKWebView(frame: window.contentView?.bounds ?? .zero, configuration: configuration)
        view.autoresizingMask = [.width, .height]
        view.navigationDelegate = self
        view.uiDelegate = self
        window.contentView = view
        catalogWebView = view
        catalogWindowController = NSWindowController(window: window)
        view.load(URLRequest(url: catalogURL))
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    @objc private func showVision() {
        DispatchQueue.main.async { [weak self] in self?.presentVision() }
    }

    private func presentVision() {
        if let window = visionWindowController?.window {
            window.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
            return
        }
        let window = NSWindow(contentRect: NSRect(x: 220, y: 180, width: 980, height: 650),
                              styleMask: [.titled, .closable, .miniaturizable, .resizable],
                              backing: .buffered, defer: false)
        window.title = "Centre Vision"
        window.minSize = NSSize(width: 680, height: 460)
        window.delegate = self
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .default()
        configuration.userContentController.add(self, name: "companion")
        let view = WKWebView(frame: window.contentView?.bounds ?? .zero, configuration: configuration)
        view.autoresizingMask = [.width, .height]
        view.navigationDelegate = self
        view.uiDelegate = self
        window.contentView = view
        visionWebView = view
        visionWindowController = NSWindowController(window: window)
        view.load(URLRequest(url: visionURL))
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    private func pythonExecutable() -> String? {
        let candidates = [
            "/opt/homebrew/bin/python3",
            "/usr/local/bin/python3",
            "/Library/Frameworks/Python.framework/Versions/Current/bin/python3",
            "/usr/bin/python3"
        ]
        return candidates.first { FileManager.default.isExecutableFile(atPath: $0) }
    }

    private func bundledScript() -> String? {
        Bundle.main.path(forResource: "ams_companion", ofType: "py")
    }

    private func engineIsReachable(completion: @escaping (Bool) -> Void) {
        var request = URLRequest(url: healthURL)
        request.timeoutInterval = 1.0
        URLSession.shared.dataTask(with: request) { data, response, _ in
            let ok = data != nil && (response as? HTTPURLResponse)?.statusCode == 200
            DispatchQueue.main.async { completion(ok) }
        }.resume()
    }

    private func engineAcceptsToken(completion: @escaping (Bool) -> Void) {
        var request = URLRequest(url: stateURL)
        request.timeoutInterval = 1.0
        request.setValue(apiToken, forHTTPHeaderField: "X-AMS-Token")
        URLSession.shared.dataTask(with: request) { _, response, _ in
            let ok = (response as? HTTPURLResponse)?.statusCode == 200
            DispatchQueue.main.async { completion(ok) }
        }.resume()
    }

    private func openEngineLog() -> FileHandle? {
        let folder = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/AMS Lite Companion V2")
        try? FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        let log = folder.appendingPathComponent("launcher.log")
        if !FileManager.default.fileExists(atPath: log.path) {
            FileManager.default.createFile(atPath: log.path, contents: nil)
        }
        guard let handle = try? FileHandle(forWritingTo: log) else { return nil }
        handle.seekToEndOfFile()
        return handle
    }

    private func startEngine(showPanel: Bool) {
        engineIsReachable { [weak self] alreadyRunning in
            guard let self = self else { return }
            if alreadyRunning {
                self.engineAcceptsToken { accepted in
                    if accepted {
                        self.statusLine.title = "Moteur connecté"
                        if showPanel { self.showPanelWhenReady(attempt: 0) }
                    } else {
                        self.statusLine.title = "Une autre instance est active"
                        self.showAlert(title: "Instance Companion déjà active",
                                       message: "Quitte l’ancienne instance Companion puis relance cette application. Cela évite des clics sans effet entre deux versions.")
                    }
                }
                return
            }
            guard let python = self.pythonExecutable(), let script = self.bundledScript() else {
                self.showAlert(title: "Python 3 est introuvable",
                               message: "Installez Python 3 avec Homebrew : brew install python")
                self.statusLine.title = "Python 3 manquant"
                return
            }

            let process = Process()
            process.executableURL = URL(fileURLWithPath: python)
            process.arguments = [script, "--no-browser", "--api-token", apiToken]
            var environment = ProcessInfo.processInfo.environment
            // The Python engine runs directly from this signed application
            // bundle. Avoid bytecode caches in Resources, which would alter
            // the sealed bundle after a normal launch.
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            process.environment = environment
            self.engineLog = self.openEngineLog()
            process.standardOutput = self.engineLog
            process.standardError = self.engineLog
            process.terminationHandler = { [weak self] _ in
                DispatchQueue.main.async {
                    guard let self = self, !self.quitting else { return }
                    self.engine = nil
                    self.engineLog?.closeFile()
                    self.engineLog = nil
                    self.statusLine.title = "Moteur arrêté — consulte le journal"
                }
            }
            do {
                try process.run()
                self.engine = process
                self.statusLine.title = "Connexion au moteur…"
                if showPanel { self.showPanelWhenReady(attempt: 0) }
            } catch {
                self.statusLine.title = "Échec du démarrage"
                self.showAlert(title: "Companion n’a pas démarré", message: error.localizedDescription)
            }
        }
    }

    private func showPanelWhenReady(attempt: Int) {
        engineAcceptsToken { [weak self] ready in
            guard let self = self else { return }
            if ready {
                self.statusLine.title = "Moteur connecté"
                self.showPanel()
            } else if attempt < 24 {
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) {
                    self.showPanelWhenReady(attempt: attempt + 1)
                }
            } else {
                self.statusLine.title = "Interface inaccessible"
                self.showAlert(title: "Interface inaccessible",
                               message: "Consultez le journal depuis le menu AMS Lite Companion V2.")
            }
        }
    }

    private func showPanel() {
        if webView.url == nil ||
            (webView.url?.host != "127.0.0.1" && webView.url?.host != "localhost") {
            webView.load(URLRequest(url: embeddedDashboardURL))
        }
        if panelDocked { dockPanelToBambuStudio() }
        panel.makeKeyAndOrderFront(nil)
        panelMenuItem.title = "Masquer le panneau Companion"
        NSApp.activate(ignoringOtherApps: true)
    }

    @objc private func togglePanel() {
        if panel.isVisible {
            panel.orderOut(nil)
            panelMenuItem.title = "Afficher le panneau Companion"
        } else {
            engineAcceptsToken { [weak self] ready in
                if ready {
                    self?.showPanel()
                } else {
                    self?.startEngine(showPanel: true)
                }
            }
        }
    }

    @objc private func toggleDocking() {
        panelDocked.toggle()
        UserDefaults.standard.set(panelDocked, forKey: "panelDocked")
        dockMenuItem.state = panelDocked ? .on : .off
        if panelDocked { dockPanelToBambuStudio() }
    }

    @objc private func openBrowserDashboard() {
        engineAcceptsToken { [weak self] ready in
            if ready {
                NSWorkspace.shared.open(dashboardURL)
            } else {
                self?.startEngine(showPanel: false)
                DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) {
                    NSWorkspace.shared.open(dashboardURL)
                }
            }
        }
    }

    @objc private func poll() {
        var request = URLRequest(url: stateURL)
        request.setValue(apiToken, forHTTPHeaderField: "X-AMS-Token")
        request.timeoutInterval = 1.5
        URLSession.shared.dataTask(with: request) { [weak self] data, response, _ in
            guard let self = self else { return }
            DispatchQueue.main.async {
                if let data = data,
                   (response as? HTTPURLResponse)?.statusCode == 200,
                   let state = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                    self.updateMenu(state)
                    self.presentMappingConfirmationIfNeeded(state)
                    self.presentSafetyAlertIfNeeded(state)
                } else {
                    self.statusLine.title = "Moteur arrêté"
                }
                self.monitorBambuStudio()
            }
        }.resume()
    }

    private func updateMenu(_ state: [String: Any]) {
        if let printer = state["printer"] as? [String: Any] {
            let connected = printer["connected"] as? Bool ?? false
            let printState = printer["state"] as? String ?? "INCONNU"
            let progress = (printer["progress"] as? NSNumber)?.intValue ?? 0
            statusLine.title = connected
                ? "Imprimante connectée · \(printState) \(progress)%"
                : "Moteur actif · imprimante déconnectée"
            panel.title = connected
                ? "AMS Lite Companion V2 · \(printState) \(progress)%"
                : "AMS Lite Companion V2"
        }
        guard let spools = state["spools"] as? [String: Any] else { return }
        for slot in 1...4 {
            guard let spool = spools[String(slot)] as? [String: Any] else { continue }
            let name = spool["name"] as? String ?? "Bobine A\(slot)"
            let remaining = (spool["remaining_g"] as? NSNumber)?.doubleValue ?? 0
            spoolLines[slot - 1].title = String(format: "A%d · %@ · %.1f g", slot, name, remaining)
        }
    }

    private func presentMappingConfirmationIfNeeded(_ state: [String: Any]) {
        guard let bridge = state["bridge"] as? [String: Any],
              bridge["mapping_confirmation_required"] as? Bool == true else {
            mappingPromptKey = nil
            return
        }
        let conflicts = bridge["mapping_conflict"] as? [[String: Any]] ?? []
        let conflictText = conflicts.compactMap { item -> String? in
            guard let filament = item["filament_id"], let saved = item["saved_slot"],
                  let bambu = item["bambu_slot"] else { return nil }
            return "Filament \(filament) : A\(saved) → A\(bambu)"
        }
        let fileKey = bridge["last_sha256"] as? String ?? ""
        let promptKey = fileKey + "|" + conflictText.joined(separator: ",")
        guard mappingPromptKey != promptKey else { return }
        mappingPromptKey = promptKey

        let alert = NSAlert()
        alert.messageText = conflictText.isEmpty
            ? "Correspondance AMS à choisir"
            : "Bambu Studio a changé la correspondance AMS"
        if conflictText.isEmpty {
            alert.informativeText = "Companion ne peut pas récupérer la correspondance AMS de Bambu Studio. Veux-tu armer ce fichier avec la correspondance enregistrée ?"
            alert.addButton(withTitle: "Utiliser la correspondance enregistrée")
            alert.addButton(withTitle: "Plus tard")
        } else {
            alert.informativeText = "\(conflictText.joined(separator: "\n"))\n\nChoisis la correspondance à utiliser pour ce décompte."
            alert.addButton(withTitle: "Utiliser Bambu Studio")
            alert.addButton(withTitle: "Garder la correspondance enregistrée")
            alert.addButton(withTitle: "Plus tard")
        }
        alert.alertStyle = .warning
        NSApp.activate(ignoringOtherApps: true)
        let result = alert.runModal()
        if result == .alertFirstButtonReturn {
            sendBridgeChoice(conflictText.isEmpty ? useSavedBridgeURL : confirmBridgeURL,
                             promptKey: promptKey)
        } else if !conflictText.isEmpty && result == .alertSecondButtonReturn {
            sendBridgeChoice(useSavedBridgeURL, promptKey: promptKey)
        }
    }

    private func presentSafetyAlertIfNeeded(_ state: [String: Any]) {
        guard let alerts = state["alerts"] as? [[String: Any]],
              let alert = alerts.first,
              let alertKey = alert["id"] as? String,
              !alertKey.isEmpty else {
            safetyPromptKey = nil
            return
        }
        guard safetyPromptKey != alertKey else { return }
        safetyPromptKey = alertKey

        let title = alert["title"] as? String ?? "Alerte Companion"
        let message = alert["message"] as? String ?? "Vérifie l’impression."
        let popup = NSAlert()
        popup.messageText = title
        popup.informativeText = "\(message)\n\nAucune commande n’est envoyée à l’imprimante."
        popup.alertStyle = .warning
        popup.addButton(withTitle: "Afficher le Gardien")
        popup.addButton(withTitle: "Plus tard")
        NSApp.activate(ignoringOtherApps: true)
        if popup.runModal() == .alertFirstButtonReturn {
            showPanel()
        }
    }

    private func sendBridgeChoice(_ url: URL, promptKey: String) {
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.httpBody = Data("{}".utf8)
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue(apiToken, forHTTPHeaderField: "X-AMS-Token")
        request.timeoutInterval = 2.0
        URLSession.shared.dataTask(with: request) { [weak self] _, response, _ in
            guard let self = self else { return }
            if (response as? HTTPURLResponse)?.statusCode != 200 {
                DispatchQueue.main.async {
                    if self.mappingPromptKey == promptKey { self.mappingPromptKey = nil }
                    self.showAlert(title: "Choix AMS non enregistré",
                                   message: "Companion n’a pas pu armer ce fichier. Réessaie depuis la fenêtre qui va réapparaître.")
                }
            }
        }.resume()
    }

    private func bambuApplication() -> NSRunningApplication? {
        NSWorkspace.shared.runningApplications.first { app in
            let name = (app.localizedName ?? "").lowercased()
            let bundle = (app.bundleIdentifier ?? "").lowercased()
            return name == "bambustudio" || name == "bambu studio" ||
                (bundle.contains("bambu") && bundle.contains("studio"))
        }
    }

    private func isBambuStudioRunning() -> Bool { bambuApplication() != nil }

    private func bambuWindowFrame() -> NSRect? {
        guard let app = bambuApplication(),
              let windows = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements],
                                                       kCGNullWindowID) as? [[String: Any]] else {
            return nil
        }
        let mainTop = NSScreen.screens.first?.frame.maxY ?? 0
        var best: NSRect?
        for info in windows {
            guard (info[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value == app.processIdentifier,
                  (info[kCGWindowLayer as String] as? NSNumber)?.intValue == 0,
                  let bounds = info[kCGWindowBounds as String] as? [String: Any],
                  let rawX = bounds["X"] as? NSNumber,
                  let rawY = bounds["Y"] as? NSNumber,
                  let rawWidth = bounds["Width"] as? NSNumber,
                  let rawHeight = bounds["Height"] as? NSNumber else { continue }
            let cgRect = CGRect(x: CGFloat(rawX.doubleValue),
                                y: CGFloat(rawY.doubleValue),
                                width: CGFloat(rawWidth.doubleValue),
                                height: CGFloat(rawHeight.doubleValue))
            let rect = NSRect(x: cgRect.minX,
                              y: mainTop - cgRect.maxY,
                              width: cgRect.width,
                              height: cgRect.height)
            if rect.width * rect.height > (best?.width ?? 0) * (best?.height ?? 0) {
                best = rect
            }
        }
        return best
    }

    private func dockPanelToBambuStudio() {
        guard panelDocked, (panel.isVisible || bambuSeen) else { return }
        let bambu = bambuWindowFrame()
        let screen = bambu.flatMap { frame in
            NSScreen.screens.first(where: { $0.frame.intersects(frame) })
        } ?? NSScreen.main
        guard let visible = screen?.visibleFrame else { return }

        let width = min(panel.frame.width, visible.width)
        let height = min(panel.frame.height, visible.height)
        var x = visible.maxX - width
        var y = visible.maxY - height
        if let bambu = bambu {
            let gap = 8.0
            if bambu.maxX + gap + width <= visible.maxX {
                x = bambu.maxX + gap
            } else if bambu.minX - gap - width >= visible.minX {
                x = bambu.minX - gap - width
            }
            y = min(max(bambu.maxY - height, visible.minY), visible.maxY - height)
        }
        panel.setFrameOrigin(NSPoint(x: x, y: y))
    }

    private func monitorBambuStudio() {
        if isBambuStudioRunning() {
            bambuSeen = true
            bambuMissingPolls = 0
            if panelDocked && panel.isVisible { dockPanelToBambuStudio() }
        } else if bambuSeen {
            bambuMissingPolls += 1
            if bambuMissingPolls >= 2 { requestQuit() }
        }
    }

    private func findBambuStudio() -> URL? {
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        let candidates = [
            "/Applications/BambuStudio.app",
            "/Applications/Bambu Studio.app",
            "\(home)/Applications/BambuStudio.app",
            "\(home)/Applications/Bambu Studio.app"
        ]
        return candidates.first(where: { FileManager.default.fileExists(atPath: $0) })
            .map { URL(fileURLWithPath: $0) }
    }

    private func launchBambuStudio() {
        guard !isBambuStudioRunning() else {
            bambuSeen = true
            return
        }
        guard let appURL = findBambuStudio() else {
            showAlert(title: "Bambu Studio officiel introuvable",
                      message: "Placez BambuStudio.app dans le dossier Applications. Companion reste disponible depuis son icône dans la barre des menus.")
            return
        }
        let configuration = NSWorkspace.OpenConfiguration()
        NSWorkspace.shared.openApplication(at: appURL, configuration: configuration) { [weak self] _, error in
            DispatchQueue.main.async {
                if let error = error {
                    self?.showAlert(title: "Impossible d’ouvrir Bambu Studio", message: error.localizedDescription)
                } else {
                    self?.bambuSeen = true
                    DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) {
                        self?.dockPanelToBambuStudio()
                    }
                }
            }
        }
    }

    @objc private func openBambu() { launchBambuStudio() }

    @objc private func restartEngine() {
        sendShutdown()
        if let process = engine, process.isRunning { process.terminate() }
        engine = nil
        webView.loadHTMLString("<html><body style='font-family:-apple-system;padding:24px'>Redémarrage du moteur…</body></html>",
                               baseURL: nil)
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.8) { [weak self] in
            self?.startEngine(showPanel: true)
        }
    }

    @objc private func openLog() {
        let log = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/AMS Lite Companion V2/companion.log")
        if FileManager.default.fileExists(atPath: log.path) {
            NSWorkspace.shared.activateFileViewerSelecting([log])
        } else {
            showAlert(title: "Journal absent", message: "Aucun journal n’a encore été créé.")
        }
    }

    private func sendShutdown() {
        var request = URLRequest(url: shutdownURL)
        request.httpMethod = "POST"
        request.httpBody = Data("{}".utf8)
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue(apiToken, forHTTPHeaderField: "X-AMS-Token")
        request.timeoutInterval = 1.0
        URLSession.shared.dataTask(with: request).resume()
    }

    @objc private func quitCompanion() { requestQuit() }

    private func requestQuit() {
        guard !quitting else { return }
        quitting = true
        statusLine.title = "Arrêt…"
        sendShutdown()
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.8) {
            NSApp.terminate(nil)
        }
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        if sender === catalogWindowController?.window {
            catalogWebView = nil
            catalogWindowController = nil
            return true
        }
        if sender === visionWindowController?.window {
            visionWebView = nil
            visionWindowController = nil
            return true
        }
        sender.orderOut(nil)
        panelMenuItem.title = "Afficher le panneau Companion"
        return false
    }

    func webView(_ webView: WKWebView,
                 decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = navigationAction.request.url else {
            decisionHandler(.cancel)
            return
        }
        if url.scheme == "about" ||
            ((url.host == "127.0.0.1" || url.host == "localhost") && url.port == 8766) {
            decisionHandler(.allow)
        } else {
            NSWorkspace.shared.open(url)
            decisionHandler(.cancel)
        }
    }

    func webView(_ webView: WKWebView,
                 runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo,
                 completionHandler: @escaping (Bool) -> Void) {
        let alert = NSAlert()
        alert.messageText = "AMS Lite Companion V2"
        alert.informativeText = message
        alert.alertStyle = .warning
        alert.addButton(withTitle: "Confirmer")
        alert.addButton(withTitle: "Annuler")
        completionHandler(alert.runModal() == .alertFirstButtonReturn)
    }

    func userContentController(_ userContentController: WKUserContentController,
                               didReceive message: WKScriptMessage) {
        guard message.name == "companion", let command = message.body as? String else { return }
        if command == "openCatalog" { showCatalog() }
        if command == "openVision" { showVision() }
        if command == "downloadCalibrationSheet" { downloadCalibrationSheet() }
    }

    private func downloadCalibrationSheet() {
        guard var components = URLComponents(string: "http://127.0.0.1:8766/api/vision/calibration-sheet.pdf") else { return }
        components.queryItems = [URLQueryItem(name: "token", value: apiToken)]
        guard let url = components.url else { return }
        URLSession.shared.dataTask(with: url) { [weak self] data, response, error in
            let success = (response as? HTTPURLResponse)?.statusCode == 200
            guard let data = data, success, data.starts(with: Data("%PDF".utf8)) else {
                DispatchQueue.main.async {
                    self?.showAlert(title: "Planche non téléchargée",
                                    message: "Companion n’a pas pu créer la planche PDF. Vérifie que le moteur est démarré puis réessaie.")
                }
                return
            }
            do {
                let downloads = FileManager.default.urls(for: .downloadsDirectory, in: .userDomainMask).first
                    ?? FileManager.default.homeDirectoryForCurrentUser
                let stamp = ISO8601DateFormatter().string(from: Date()).replacingOccurrences(of: ":", with: "-")
                let destination = downloads.appendingPathComponent("ams-companion-calibration-180mm-\(stamp).pdf")
                try data.write(to: destination, options: .atomic)
                DispatchQueue.main.async {
                    self?.showAlert(title: "Planche enregistrée",
                                    message: "La planche PDF est dans Téléchargements. Imprime-la à 100 %, sans ajustement.")
                    NSWorkspace.shared.activateFileViewerSelecting([destination])
                }
            } catch {
                DispatchQueue.main.async {
                    self?.showAlert(title: "Planche non enregistrée",
                                    message: "Impossible d’écrire dans Téléchargements : \(error.localizedDescription)")
                }
            }
        }.resume()
    }

    private func showAlert(title: String, message: String) {
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = message
        alert.alertStyle = .warning
        alert.runModal()
    }
}

let application = NSApplication.shared
let delegate = AppDelegate()
application.delegate = delegate
application.setActivationPolicy(.accessory)
application.run()
