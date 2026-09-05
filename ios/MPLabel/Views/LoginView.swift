//  LoginView.swift
//
//  Two screens that only appear when something is missing: where the
//  server is, and who she is.

import SwiftUI

struct ServerSetupView: View {
    @EnvironmentObject private var session: Session
    @State private var address = ""

    var body: some View {
        VStack(spacing: 18) {
            Spacer()
            Image(systemName: "shippingbox")
                .font(.system(size: 44))
                .foregroundStyle(Color.mpAccent)
            Text("Where is it?").font(.title2).bold()
            Text("The address of the machine running `mplabel serve`.")
                .font(.footnote)
                .foregroundStyle(Color.mpMuted)
                .multilineTextAlignment(.center)

            TextField("mplabel.example.com", text: $address)
                .textFieldStyle(.roundedBorder)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .keyboardType(.URL)

            // https:// is added if she leaves it off, but plain http is
            // accepted for a LAN address during development. Worth
            // knowing which she is on: over http the token and the
            // password cross the network in clear.
            Text("https:// is assumed unless you type otherwise.")
                .font(.caption2)
                .foregroundStyle(Color.mpMuted)

            Button("Continue") {
                Settings.serverURL = address
                session.refresh()
            }
            .buttonStyle(.borderedProminent)
            .disabled(address.trimmingCharacters(in: .whitespaces).isEmpty)
            Spacer()
        }
        .padding(28)
    }
}

struct LoginView: View {
    @EnvironmentObject private var session: Session
    @State private var password = ""
    @State private var error: String?
    @State private var busy = false

    var body: some View {
        VStack(spacing: 18) {
            Spacer()
            Text("mplabel").font(.largeTitle).bold()
            Text(Settings.serverURL ?? "")
                .font(.caption)
                .foregroundStyle(Color.mpMuted)

            SecureField("Password", text: $password)
                .textFieldStyle(.roundedBorder)
                .textContentType(.password)
                .onSubmit(signIn)

            if let error {
                Text(error).font(.footnote).foregroundStyle(Color.mpAlert)
                    .multilineTextAlignment(.center)
            }

            Button(action: signIn) {
                if busy { ProgressView() } else { Text("Sign in") }
            }
            .buttonStyle(.borderedProminent)
            .disabled(busy || password.isEmpty)

            Button("Change server") {
                Settings.serverURL = nil
                session.refresh()
            }
            .font(.footnote)
            Spacer()
        }
        .padding(28)
    }

    private func signIn() {
        guard !busy, !password.isEmpty else { return }
        busy = true
        error = nil
        Task {
            do {
                try await APIClient.shared.logIn(password: password)
                password = ""
                session.refresh()
            } catch {
                // The server locks out after five failures for five
                // minutes, and says so. Show its words rather than a
                // generic "wrong password" that contradicts them.
                self.error = error.localizedDescription
            }
            busy = false
        }
    }
}

struct SettingsView: View {
    @EnvironmentObject private var session: Session

    var body: some View {
        NavigationStack {
            List {
                Section("Server") {
                    Text(Settings.serverURL ?? "not set")
                        .font(.system(.footnote, design: .monospaced))
                    Button("Change server") {
                        Settings.serverURL = nil
                        session.signOut()
                    }
                }
                Section {
                    Button("Sign out", role: .destructive) { session.signOut() }
                } footer: {
                    Text("Printer settings are not here. They live in "
                         + "/etc/mplabel.conf on the machine with the "
                         + "printer, which is the one that can see the paper.")
                }
            }
            .navigationTitle("Settings")
        }
    }
}
