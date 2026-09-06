//  LoginView.swift
//
//  Two screens that only appear when something is missing: where the
//  server is, and who she is. Plus settings, which is mostly a place to
//  say what is deliberately not configurable from here.

import SwiftUI

struct ServerSetupView: View {
    @EnvironmentObject private var session: Session
    @State private var address = ""

    var body: some View {
        VStack(spacing: MP.S.x4) {
            Spacer()
            Image(systemName: "shippingbox")
                .font(.system(size: 40))
                .foregroundStyle(MP.Palette.accent)
            MPEyebrow("Day one")
            Text("Where is it?")
                .font(.system(size: 26, weight: .semibold))
                .foregroundStyle(MP.Palette.fg)
            Text("The address of the machine running mplabel serve.")
                .font(.system(size: 13))
                .foregroundStyle(MP.Palette.muted)
                .multilineTextAlignment(.center)

            TextField("mplabel.example.com", text: $address)
                .textFieldStyle(.plain)
                .font(.system(size: 16))
                .padding(MP.S.x3)
                .background(MP.Palette.raised,
                            in: RoundedRectangle(cornerRadius: MP.R.card))
                .overlay(RoundedRectangle(cornerRadius: MP.R.card)
                    .strokeBorder(MP.Palette.border, lineWidth: 1))
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .keyboardType(.URL)

            // https:// is added if she leaves it off, but plain http is
            // accepted for a LAN address during development. Worth
            // knowing which she is on: over http the token and the
            // password cross the network in clear.
            Text("https:// is assumed unless you type otherwise.")
                .font(.system(size: 11.5))
                .foregroundStyle(MP.Palette.subtle)

            Button {
                Settings.serverURL = address
                session.refresh()
            } label: {
                Text("Continue")
                    .font(.system(size: 15, weight: .semibold))
                    .foregroundStyle(MP.Palette.accentInk)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, MP.S.x3)
                    .background(MP.Palette.accent,
                                in: RoundedRectangle(cornerRadius: MP.R.card))
            }
            .disabled(address.trimmingCharacters(in: .whitespaces).isEmpty)
            Spacer()
        }
        .padding(MP.S.x6)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(MP.Palette.bg)
    }
}

struct LoginView: View {
    @EnvironmentObject private var session: Session
    @State private var password = ""
    @State private var error: String?
    @State private var busy = false

    var body: some View {
        VStack(spacing: MP.S.x4) {
            Spacer()
            Text("mplabel")
                .font(.system(size: 32, weight: .semibold))
                .foregroundStyle(MP.Palette.fg)
            Text(Settings.serverURL ?? "")
                .font(.system(size: 11.5).monospaced())
                .foregroundStyle(MP.Palette.subtle)

            SecureField("Password", text: $password)
                .textFieldStyle(.plain)
                .font(.system(size: 16))
                .padding(MP.S.x3)
                .background(MP.Palette.raised,
                            in: RoundedRectangle(cornerRadius: MP.R.card))
                .overlay(RoundedRectangle(cornerRadius: MP.R.card)
                    .strokeBorder(MP.Palette.border, lineWidth: 1))
                .textContentType(.password)
                .onSubmit(signIn)

            if let error { MPError(message: error) }

            Button(action: signIn) {
                Group {
                    if busy { ProgressView() } else { Text("Sign in") }
                }
                .font(.system(size: 15, weight: .semibold))
                .foregroundStyle(MP.Palette.accentInk)
                .frame(maxWidth: .infinity)
                .padding(.vertical, MP.S.x3)
                .background(MP.Palette.accent,
                            in: RoundedRectangle(cornerRadius: MP.R.card))
            }
            .disabled(busy || password.isEmpty)

            Button("Change server") {
                Settings.serverURL = nil
                session.refresh()
            }
            .font(.system(size: 12))
            .foregroundStyle(MP.Palette.muted)
            Spacer()
        }
        .padding(MP.S.x6)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(MP.Palette.bg)
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
            MPScreen(eyebrow: "Yours", title: "Settings") {
                MPCard {
                    VStack(alignment: .leading, spacing: MP.S.x2) {
                        MPEyebrow("Server")
                        Text(Settings.serverURL ?? "not set")
                            .font(.system(size: 12).monospaced())
                            .foregroundStyle(MP.Palette.fg)
                    }
                }

                Button {
                    Settings.serverURL = nil
                    session.signOut()
                } label: {
                    MPCard {
                        Text("Change server")
                            .font(.system(size: 14))
                            .foregroundStyle(MP.Palette.fg)
                    }
                }
                .buttonStyle(.plain)

                Button { session.signOut() } label: {
                    MPCard {
                        Text("Sign out")
                            .font(.system(size: 14))
                            .foregroundStyle(MP.Palette.alert)
                    }
                }
                .buttonStyle(.plain)

                Text("Printer settings are not here. They live in "
                     + "/etc/mplabel.conf on the machine with the printer, "
                     + "which is the one that can see the paper.")
                    .font(.system(size: 11.5))
                    .foregroundStyle(MP.Palette.subtle)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.top, MP.S.x2)
            }
        }
    }
}
