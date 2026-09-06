//  CaptureView.swift
//
//  "I'm in a Goodwill holding a receipt." One hand on the cart, phone in
//  the other. The whole screen is a shutter: no form, no trip to pick,
//  nothing to fill in before the picture exists.
//
//  That ordering is the design's and it is the right way round. What she
//  is holding - a paper receipt, a thing on a shelf - stops being
//  available the moment she walks out; the attribution can be done at
//  the kitchen table and is what TriageView is for. A form first would
//  trade the irrecoverable half for the recoverable one.
//
//  Uploads go straight up rather than into a queue on the phone. The
//  server keys a photo on the sha256 of its bytes, so pressing the
//  button again after a failure cannot make a second row - which is
//  what makes "retry" the whole of the error handling here, and why a
//  local outbox would be a second source of truth rather than a
//  feature. If she is somewhere with no signal at all, the shots that
//  failed say so and stay on screen to be sent from the car park.

import AVFoundation
import SwiftUI
import UIKit

struct CaptureView: View {
    /// Permission is four states, not a Bool - the same lesson ScanView
    /// paid for. "Not asked yet" collapsed into "no" is a blank screen.
    private enum Access {
        case checking
        case granted
        case denied
        case unsupported
    }

    /// One press of the shutter, and where it got to. The image is kept
    /// so a failed upload can be retried without asking her to take the
    /// photograph again - which she cannot, having left the shop.
    private struct Shot: Identifiable {
        let id = UUID()
        let data: Data
        var photo: Photo?
        var failure: String?
        var sending: Bool

        var done: Bool { photo != nil }
    }

    @State private var access: Access = .checking
    @State private var shots: [Shot] = []
    @State private var waiting = 0
    @State private var error: String?
    @State private var showingTriage = false

    var body: some View {
        NavigationStack {
            Group {
                switch access {
                case .checking:   ProgressView().task { await check() }
                case .granted:    camera
                case .denied:     denied
                case .unsupported: unsupported
                }
            }
            .navigationTitle("Capture")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button { showingTriage = true } label: {
                        Text(waiting > 0 ? "\(waiting) to triage" : "Triage")
                    }
                    .disabled(waiting == 0)
                }
            }
            .navigationDestination(isPresented: $showingTriage) {
                TriageView()
            }
            .task { await countWaiting() }
        }
    }

    // MARK: - the states

    private var camera: some View {
        ZStack(alignment: .bottom) {
            CameraStill(onCapture: keep, onFailure: { error = $0 })
                .ignoresSafeArea()

            VStack(spacing: MP.S.x2) {
                if let error { MPError(message: error) }
                if !shots.isEmpty { strip }
                shutter
            }
            .padding(.bottom, MP.S.x3)
        }
        .background(Color.black)
    }

    /// What has been taken this visit, and what became of it. Deliberately
    /// on screen rather than in a toast: an upload that failed silently
    /// is a receipt she thinks she has.
    private var strip: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: MP.S.x2) {
                ForEach(shots) { shot in
                    Button {
                        if shot.failure != nil { retry(shot) }
                    } label: {
                        thumbnail(shot)
                    }
                    .disabled(shot.failure == nil)
                }
            }
            .padding(.horizontal, MP.S.x3)
        }
        .frame(height: 72)
    }

    private func thumbnail(_ shot: Shot) -> some View {
        ZStack(alignment: .bottomTrailing) {
            if let image = UIImage(data: shot.data) {
                Image(uiImage: image)
                    .resizable()
                    .scaledToFill()
                    .frame(width: 56, height: 64)
                    .clipShape(RoundedRectangle(cornerRadius: MP.R.sm))
            }
            Group {
                if shot.sending {
                    ProgressView().scaleEffect(0.6)
                } else if shot.failure != nil {
                    Image(systemName: "arrow.clockwise.circle.fill")
                        .foregroundStyle(MP.Palette.alert)
                } else {
                    Image(systemName: "checkmark.circle.fill")
                        .foregroundStyle(MP.Palette.accent)
                }
            }
            .font(.system(size: 15))
            .padding(3)
        }
        .overlay(RoundedRectangle(cornerRadius: MP.R.sm)
            .stroke(shot.failure != nil ? MP.Palette.alert : .clear,
                    lineWidth: 1.5))
    }

    private var shutter: some View {
        HStack(alignment: .center, spacing: MP.S.x4) {
            Text(hint)
                .font(.system(size: 11.5))
                .foregroundStyle(.white.opacity(0.85))
                .frame(maxWidth: .infinity, alignment: .leading)

            // Not an MPHoldButton. Holding is the guard on actions that
            // spend paper or close a sale; a photograph is free and the
            // hand holding the phone is also holding a cart.
            Button(action: { NotificationCenter.default.post(
                name: .mplabelShutter, object: nil) }) {
                Circle()
                    .fill(.white)
                    .frame(width: 66, height: 66)
                    .overlay(Circle().stroke(.white.opacity(0.6), lineWidth: 3)
                        .padding(-6))
            }
            .accessibilityLabel("Take a photo")

            Text(shots.isEmpty ? "" : "\(shots.count) this visit")
                .font(.system(size: 11.5))
                .foregroundStyle(.white.opacity(0.85))
                .frame(maxWidth: .infinity, alignment: .trailing)
        }
        .padding(.horizontal, MP.S.x4)
    }

    private var hint: String {
        let failed = shots.filter { $0.failure != nil }.count
        if failed > 0 { return "\(failed) did not send - tap to retry" }
        return "Receipt or the thing itself"
    }

    private var denied: some View {
        ContentUnavailableView {
            Label("No camera access", systemImage: "camera.slash")
        } description: {
            Text("Capture is the camera and nothing else. Turn it on in "
                 + "Settings, or add the item by hand from the Shelf tab.")
        } actions: {
            Button("Open Settings") {
                if let url = URL(string: UIApplication.openSettingsURLString) {
                    UIApplication.shared.open(url)
                }
            }
        }
    }

    /// The simulator has no camera. Same words as the scanner uses, for
    /// the same reason: this is an ordinary situation, not a fault.
    private var unsupported: some View {
        ContentUnavailableView {
            Label("No camera here", systemImage: "camera.slash")
        } description: {
            Text("Photographs need a real device. The triage queue still "
                 + "works - anything already captured is waiting in it.")
        } actions: {
            Button("Go to triage") { showingTriage = true }
        }
    }

    // MARK: - doing the work

    private func check() async {
        #if targetEnvironment(simulator)
        access = .unsupported
        #else
        guard AVCaptureDevice.default(for: .video) != nil else {
            access = .unsupported
            return
        }
        switch AVCaptureDevice.authorizationStatus(for: .video) {
        case .authorized:
            access = .granted
        case .notDetermined:
            access = await AVCaptureDevice.requestAccess(for: .video)
                ? .granted : .denied
        default:
            access = .denied
        }
        #endif
    }

    private func countWaiting() async {
        waiting = (try? await APIClient.shared.untriaged().count) ?? 0
    }

    private func keep(_ data: Data) {
        var shot = Shot(data: data, photo: nil, failure: nil, sending: true)
        shots.append(shot)
        shot.sending = true
        send(shot)
    }

    private func retry(_ shot: Shot) {
        guard let index = shots.firstIndex(where: { $0.id == shot.id }) else {
            return
        }
        shots[index].failure = nil
        shots[index].sending = true
        send(shots[index])
    }

    private func send(_ shot: Shot) {
        Task {
            do {
                let photo = try await APIClient.shared.uploadPhoto(shot.data)
                update(shot) { $0.photo = photo; $0.sending = false }
                await countWaiting()
            } catch {
                // Keep the bytes. She is not going back to the shop.
                update(shot) {
                    $0.failure = error.localizedDescription
                    $0.sending = false
                }
            }
        }
    }

    private func update(_ shot: Shot, _ change: (inout Shot) -> Void) {
        guard let index = shots.firstIndex(where: { $0.id == shot.id }) else {
            return
        }
        change(&shots[index])
    }
}

extension Notification.Name {
    /// The shutter is a SwiftUI button and the capture session lives in a
    /// UIKit controller; this is the one message between them.
    static let mplabelShutter = Notification.Name("mplabel.shutter")
}

/// A still camera, which is not what `DataScannerViewController` is.
///
/// `AVCapturePhotoOutput` rather than a `UIImagePickerController`,
/// because the picker's own review-and-confirm step is exactly the
/// friction this screen exists to remove: she is standing up, holding a
/// cart, and the next thing she wants is the next photograph.
struct CameraStill: UIViewControllerRepresentable {
    let onCapture: (Data) -> Void
    let onFailure: (String) -> Void

    func makeCoordinator() -> Coordinator {
        Coordinator(onCapture: onCapture, onFailure: onFailure)
    }

    func makeUIViewController(context: Context) -> CameraController {
        CameraController(coordinator: context.coordinator)
    }

    func updateUIViewController(_ vc: CameraController, context: Context) {}

    final class Coordinator: NSObject, AVCapturePhotoCaptureDelegate {
        let onCapture: (Data) -> Void
        let onFailure: (String) -> Void

        init(onCapture: @escaping (Data) -> Void,
             onFailure: @escaping (String) -> Void) {
            self.onCapture = onCapture
            self.onFailure = onFailure
        }

        func photoOutput(_ output: AVCapturePhotoOutput,
                         didFinishProcessingPhoto photo: AVCapturePhoto,
                         error: Error?) {
            if let error {
                onFailure(error.localizedDescription)
                return
            }
            guard let data = photo.fileDataRepresentation() else {
                onFailure("the camera returned no image")
                return
            }
            onCapture(data)
        }
    }
}

final class CameraController: UIViewController {
    private let session = AVCaptureSession()
    private let output = AVCapturePhotoOutput()
    private let coordinator: CameraStill.Coordinator
    private var observer: NSObjectProtocol?

    init(coordinator: CameraStill.Coordinator) {
        self.coordinator = coordinator
        super.init(nibName: nil, bundle: nil)
    }

    required init?(coder: NSCoder) { fatalError("not from a nib") }

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .black

        session.beginConfiguration()
        session.sessionPreset = .photo
        guard let device = AVCaptureDevice.default(for: .video),
              let input = try? AVCaptureDeviceInput(device: device),
              session.canAddInput(input), session.canAddOutput(output) else {
            session.commitConfiguration()
            coordinator.onFailure("the camera could not be opened")
            return
        }
        session.addInput(input)
        session.addOutput(output)
        session.commitConfiguration()

        let preview = AVCaptureVideoPreviewLayer(session: session)
        preview.videoGravity = .resizeAspectFill
        preview.frame = view.bounds
        preview.name = "preview"
        view.layer.addSublayer(preview)

        observer = NotificationCenter.default.addObserver(
            forName: .mplabelShutter, object: nil, queue: .main) { [weak self] _ in
                self?.shoot()
            }

        // Off the main thread: starting a capture session blocks, and on
        // a phone that is a visible stall on the screen she just opened.
        Task.detached { [session] in session.startRunning() }
    }

    override func viewDidLayoutSubviews() {
        super.viewDidLayoutSubviews()
        view.layer.sublayers?
            .first { $0.name == "preview" }?
            .frame = view.bounds
    }

    override func viewDidDisappear(_ animated: Bool) {
        super.viewDidDisappear(animated)
        // A running camera in the background is a battery and a privacy
        // light for no reason.
        Task.detached { [session] in session.stopRunning() }
    }

    deinit {
        if let observer { NotificationCenter.default.removeObserver(observer) }
    }

    private func shoot() {
        guard session.isRunning else { return }
        output.capturePhoto(with: AVCapturePhotoSettings(),
                            delegate: coordinator)
    }
}
