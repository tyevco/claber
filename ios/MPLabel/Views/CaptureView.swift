//  CaptureView.swift
//
//  "I'm in a Goodwill holding a receipt." One hand on the cart, phone in
//  the other. The whole screen is a shutter: no form, no trip to pick,
//  nothing to fill in before the picture exists.
//
//  That ordering is the design's and it is the right way round. What she
//  is holding - a paper receipt, a thing on a shelf - stops being
//  available the moment she walks out; the attribution can be done at
//  the kitchen table and is what ReconcileView is for. A form first would
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
        /// What the phone made of it, and what she decided. The analysis
        /// runs on every shot without being asked: she is holding a cart
        /// and the answer wants to be there by the time she looks down,
        /// not one tap later.
        var suggested: OnDevice.Suggested?
        var thinking = false
        var candidate: Candidate?
        var decision: String?

        var done: Bool { photo != nil }
    }

    @State private var access: Access = .checking
    @State private var shots: [Shot] = []
    @State private var waiting = 0
    @State private var error: String?
    @State private var showingTriage = false
    /// The run these belong to. A candidate with no trip cannot be
    /// reconciled against a receipt later, so this is asked for once at
    /// the start rather than inferred - "which shop is this" is a
    /// question she can answer in the car park and nothing else can.
    @State private var run: Trip?
    @State private var runs: [Trip] = []
    @State private var newStore = ""

    var body: some View {
        NavigationStack {
            Group {
                switch access {
                case .checking:   ProgressView().task { await check() }
                case .granted:    camera
                case .denied:     denied
                case .unsupported:
                    // The chooser belongs here too. A phone with no
                    // camera can still reconcile a run, and the
                    // simulator is exactly that phone - so a chooser
                    // that lives only in the viewfinder is unreachable
                    // on the one device the tests run on.
                    VStack(spacing: MP.S.x3) {
                        unsupported
                        if run == nil { runChooser }
                    }
                }
            }
            .navigationTitle(run?.store ?? "Capture")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button { showingTriage = true } label: {
                        Text(waiting > 0 ? "\(waiting) in the cart"
                                         : "Reconcile")
                    }
                    // Only with a run to reconcile *against*. The receipt
                    // and the cart have to be the same shop trip or
                    // there is nothing to lay side by side.
                    .disabled(run == nil)
                }
            }
            .navigationDestination(isPresented: $showingTriage) {
                if let run { ReconcileView(trip: run) }
            }
            .task { await loadRuns() }
        }
    }

    // MARK: - the states

    /// Which shop, asked once.
    ///
    /// A candidate with no trip cannot be reconciled against a receipt
    /// later, and nothing can infer the shop from a photograph of a
    /// vase. Today's runs are offered first because the common case is
    /// walking back in after putting a box in the car.
    /// Which shop, inline.
    ///
    /// This was a sheet and then a pushed screen, and both fought the
    /// state it depends on: the runs arrive from the Pi *after* the
    /// screen is up, the parent re-renders when they land, and the
    /// presentation went with it. Inline has no presentation to lose -
    /// and it only appears when there is no run, which is exactly when
    /// she needs to answer the question.
    private var runChooser: some View {
        VStack(alignment: .leading, spacing: MP.S.x2) {
            Text("Which shop is this?")
                .font(.system(size: 13, weight: .semibold))
                .foregroundStyle(MP.Palette.fg)
            Text("A run is a shop and a day. Everything photographed "
                 + "attaches to it, and the receipt is reconciled against "
                 + "it later.")
                .font(.system(size: 11.5))
                .foregroundStyle(MP.Palette.muted)
            if let error { MPError(message: error) }

            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: MP.S.x2) {
                    ForEach(runs) { trip in
                        Button {
                            run = trip
                            Task { await countWaiting() }
                        } label: {
                            VStack(alignment: .leading, spacing: 1) {
                                Text(trip.store)
                                    .font(.system(size: 13, weight: .semibold))
                                Text(trip.occurredAt ?? "—")
                                    .font(.system(size: 10.5))
                                    .foregroundStyle(MP.Palette.muted)
                            }
                            .padding(.horizontal, MP.S.x3)
                            .padding(.vertical, MP.S.x2)
                            .background(MP.Palette.raised,
                                        in: RoundedRectangle(
                                            cornerRadius: MP.R.chip))
                        }
                        .buttonStyle(.plain)
                        // Named, because SwiftUI collapses a button's
                        // children into one element and the shop's name
                        // is then addressable by nothing - which is how
                        // a person finds this row too.
                        .accessibilityLabel(trip.store)
                    }
                }
            }
            HStack(spacing: MP.S.x2) {
                TextField("A new shop", text: $newStore)
                    .font(.system(size: 14))
                    .textInputAutocapitalization(.characters)
                    .accessibilityIdentifier("run-store")
                Button("Start") { startRun() }
                    .font(.system(size: 13, weight: .semibold))
                    .disabled(newStore.trimmingCharacters(
                        in: .whitespaces).isEmpty)
            }
        }
        .padding(MP.S.x3)
        .background(MP.Palette.bg,
                    in: RoundedRectangle(cornerRadius: MP.R.card))
        .padding(.horizontal, MP.S.x3)
        .task { await loadRuns() }
    }

    private var camera: some View {
        ZStack(alignment: .bottom) {
            CameraStill(onCapture: keep, onFailure: { error = $0 })
                .ignoresSafeArea()

            VStack(spacing: MP.S.x2) {
                if let error { MPError(message: error) }
                // The most recent undecided shot, with what the phone
                // made of it. One at a time: she is looking at the
                // object, not at a list, and the decision is about the
                // thing in her hands.
                if let pending = shots.last(where: { $0.decision == nil }) {
                    decisionCard(pending)
                }
                if !shots.isEmpty { strip }
                if run == nil {
                    runChooser
                } else if let run {
                    Text("Run: " + run.store)
                        .font(.system(size: 11.5, weight: .semibold))
                        .foregroundStyle(.white.opacity(0.85))
                        .padding(.horizontal, MP.S.x3)
                        .padding(.vertical, MP.S.x1)
                        .background(.black.opacity(0.45), in: Capsule())
                }
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

    /// Cart it or put it back, with whatever the phone worked out.
    ///
    /// The analysis is a starting point and says so - it is wrong often
    /// enough that presenting it as a finding would train her to ignore
    /// it. What matters is the decision, which is hers and takes one
    /// tap either way.
    private func decisionCard(_ shot: Shot) -> some View {
        VStack(alignment: .leading, spacing: MP.S.x2) {
            if shot.thinking {
                HStack(spacing: MP.S.x2) {
                    ProgressView().scaleEffect(0.7)
                    Text("Looking at it…")
                        .font(.system(size: 12.5))
                        .foregroundStyle(.white.opacity(0.9))
                }
            } else if let s = shot.suggested {
                Text(s.title.isEmpty ? "Not sure what this is" : s.title)
                    .font(.system(size: 15, weight: .semibold))
                    .foregroundStyle(.white)
                    .lineLimit(2)
                let detail = [s.era, s.condition]
                    .filter { !$0.isEmpty }
                    .joined(separator: " · ")
                if !detail.isEmpty {
                    Text(detail)
                        .font(.system(size: 12))
                        .foregroundStyle(.white.opacity(0.8))
                        .lineLimit(2)
                }
            }
            HStack(spacing: MP.S.x2) {
                Button { decide(shot, "passed") } label: {
                    Text("Put it back")
                        .font(.system(size: 14, weight: .semibold))
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, MP.S.x3)
                        .background(.white.opacity(0.15),
                                    in: RoundedRectangle(cornerRadius: MP.R.chip))
                        .foregroundStyle(.white)
                }
                Button { decide(shot, "carted") } label: {
                    Text("In the cart")
                        .font(.system(size: 14, weight: .semibold))
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, MP.S.x3)
                        .background(MP.Palette.accent,
                                    in: RoundedRectangle(cornerRadius: MP.R.chip))
                        .foregroundStyle(MP.Palette.accentInk)
                }
            }
            .buttonStyle(.plain)
        }
        .padding(MP.S.x3)
        .background(.black.opacity(0.55),
                    in: RoundedRectangle(cornerRadius: MP.R.card))
        .padding(.horizontal, MP.S.x3)
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
            Button("Go to the cart") { showingTriage = true }
                .disabled(run == nil)
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

    /// How many things are in the cart for this run - which is what the
    /// reconcile screen is about. Was the untriaged photo pile, which
    /// counted receipts as things to attribute and was the wrong number
    /// for this flow.
    private func countWaiting() async {
        guard let run else { waiting = 0; return }
        waiting = (try? await APIClient.shared.candidates(
            trip: run.id, decision: "carted").count) ?? 0
    }

    private func loadRuns() async {
        // Not `try?`. A swallowed failure here is indistinguishable from
        // "no runs yet", and the two want opposite responses from her -
        // one is a shop to type in, the other is a Pi that is not
        // answering. This was exactly that bug: the load was being
        // cancelled when `check()` flipped the screen out from under it,
        // and the sheet said there were no runs.
        do {
            runs = try await APIClient.shared.trips()
            error = nil
        } catch is CancellationError {
            return                      // the view moved on; not a failure
        } catch {
            self.error = error.localizedDescription
            return
        }
        // Today's, if there is one - walking back in after putting a box
        // in the car is the common case, and asking again would be a
        // second run for one shop.
        let today = ISO8601DateFormatter()
        today.formatOptions = [.withFullDate]
        let stamp = today.string(from: Date())
        run = run ?? runs.first { $0.occurredAt == stamp }
        await countWaiting()
    }

    private func startRun() {
        let store = newStore.trimmingCharacters(in: .whitespaces)
        Task {
            do {
                let made = try await APIClient.shared.makeTrip(store: store)
                run = made
                runs.insert(made, at: 0)
                newStore = ""
                await countWaiting()
            } catch {
                self.error = error.localizedDescription
            }
        }
    }

    /// Runs on every shot, without being asked.
    ///
    /// She is holding a cart and the answer wants to be there by the time
    /// she looks down. The cost is a model run on things she immediately
    /// puts back - which is most of them, and is the right trade: the
    /// analysis is worth most exactly when she has not decided yet.
    private func analyse(_ shot: Shot) {
        guard #available(iOS 27.0, *), OnDevice.readiness.canGenerate,
              let cg = UIImage(data: shot.data)?.cgImage else { return }
        update(shot) { $0.thinking = true }
        Task {
            let found = try? await OnDevice.suggestions(from: cg)
            update(shot) {
                $0.suggested = found
                $0.thinking = false
            }
        }
    }

    private func decide(_ shot: Shot, _ decision: String) {
        update(shot) { $0.decision = decision }
        Task {
            do {
                // The candidate is created at the moment of the
                // decision, not at the moment of the photograph: what
                // makes it worth a row is that she looked at it and said
                // something, and a shot she never decided about is just
                // a picture.
                let made = try await APIClient.shared.addCandidate(
                    trip: run?.id, photo: shot.photo?.id,
                    title: shot.suggested?.title,
                    era: shot.suggested?.era,
                    condition: shot.suggested?.condition,
                    category: shot.suggested?.category)
                try await APIClient.shared.decide(candidate: made.id,
                                                  decision)
                update(shot) { $0.candidate = made }
            } catch {
                // Put the decision back so she can try again - a failed
                // one that looked accepted is a thing in the cart the
                // reconcile screen will never mention.
                update(shot) { $0.decision = nil }
                self.error = error.localizedDescription
            }
        }
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
        analyse(shot)
        Task {
            do {
                let photo = try await APIClient.shared.uploadPhoto(
                    shot.data, trip: run?.id)
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
