//  OnDevice.swift
//
//  Apple's on-device model, used for the two places the design asks for
//  help writing something down: a draft listing, and suggestions off a
//  photograph.
//
//  **On-device is the whole reason this is acceptable here.** The
//  alternative is posting photographs of her stock, her prices and what
//  she paid to somebody's API, which would be the first thing in this
//  project to leave the house. `FoundationModels` needs no key, no
//  vendor, no network - which also deletes the design's "no signal in
//  the store, they'll fill in when you're back on wifi" state, because
//  there is nothing to wait for.
//
//  Two rules hold this down, and both are about the failure this repo
//  keeps paying for - a plausible wrong value that nothing flags:
//
//  1. **It never suggests what something cost.** `paid` is not in the
//     generated type at all, so there is no path from a guess to a
//     margin. Every number in the analytics is derived from that field;
//     a hallucinated 4.99 would be indistinguishable from a real one
//     for ever.
//  2. **Nothing it produces is written anywhere on its own.** The caller
//     applies a suggestion when she taps it, one field at a time. A
//     model that fills a form in is a model whose mistakes she has to
//     notice; a model that offers chips is one she has to agree with.

import Foundation
import FoundationModels
import CoreGraphics

enum OnDevice {

    /// Four answers, not a Bool - the same shape camera permission needed,
    /// and for the same reason: "not enabled yet" and "this phone cannot"
    /// are different sentences to put in front of her, and collapsing
    /// them into `false` produces a dead button with no explanation.
    enum Readiness: Equatable {
        case ready
        /// Apple Intelligence is off in Settings. She can fix this.
        case notEnabled
        /// This phone cannot run it. She cannot fix this.
        case notEligible
        /// Downloading or warming up. Worth trying again shortly.
        case warming
        case unavailable(String)

        var canGenerate: Bool { self == .ready }

        /// What to put on screen. Never "unavailable" on its own.
        var sentence: String {
            switch self {
            case .ready:
                return ""
            case .notEnabled:
                return "Apple Intelligence is turned off. Turn it on in "
                     + "Settings to get a draft here."
            case .notEligible:
                return "This phone cannot run the on-device model, so "
                     + "there is no draft. Everything else works."
            case .warming:
                return "The model is still getting ready. Try again in a "
                     + "moment."
            case .unavailable(let why):
                return why
            }
        }
    }

    static var readiness: Readiness {
        switch SystemLanguageModel.default.availability {
        case .available:
            return .ready
        case .unavailable(let reason):
            switch reason {
            case .appleIntelligenceNotEnabled: return .notEnabled
            case .deviceNotEligible:           return .notEligible
            case .modelNotReady:               return .warming
            @unknown default:
                return .unavailable("The on-device model is not available "
                                    + "on this phone right now.")
            }
        }
    }

    // MARK: - what it is allowed to write

    /// What a photograph is allowed to fill in. Note what is **not**
    /// here: `paid`. See the header.
    ///
    /// Every field is a plain `String` rather than an Optional because an
    /// empty answer is the model's way of declining, and "" is easier to
    /// treat as "no suggestion" at one place in the caller than an
    /// Optional is to thread through a form.
    @Generable
    struct Suggested: Equatable {
        @Guide(description: """
            A short, specific title for a second-hand item, the way it \
            would be listed for sale. Name the object and its material \
            if it is obvious. No price, no condition, no sales language.
            """)
        var title: String

        @Guide(description: """
            Roughly when the object is from, as a person would write it - \
            "c. 1910", "mid-century", "1970s". Leave this empty unless \
            the object clearly shows its period. A guess here is worse \
            than nothing.
            """)
        var era: String

        @Guide(description: """
            What is visibly wrong with it - a chip, a crack, crazing, \
            wear. Empty if nothing is visible. Do not describe what is \
            fine about it.
            """)
        var condition: String

        @Guide(description: """
            One broad shopping category, such as Home, Furniture, \
            Clothing, Tools, Toys.
            """)
        var category: String
    }

    /// What she typed, as the model gets to see it. A struct rather than
    /// a formatted string so the prompt is built in one place and the
    /// tests can hand it fields without a view.
    struct Item: Equatable {
        var title: String
        var era: String
        var condition: String
        var asking: String

        var isEmpty: Bool {
            [title, era, condition].allSatisfy {
                $0.trimmingCharacters(in: .whitespaces).isEmpty
            }
        }
    }

    // MARK: - the listing kit

    /// Note what these do **not** say: who is selling.
    ///
    /// The first version opened "…sold on Facebook Marketplace by one
    /// person from her home", which is true, reads as helpful context,
    /// and made the safety classifier refuse the whole request with
    /// `guardrailViolation` - on a milk glass vase with a chip in it.
    /// Measured by isolating one sentence at a time: dropping the
    /// negative list did not help, dropping "Facebook Marketplace" did
    /// not help, dropping the description of *her* did. Describing a
    /// private individual is what tripped it.
    ///
    /// So the instructions say what to write and nothing about who wants
    /// it written. The model does not need to know, and telling it costs
    /// the whole feature intermittently.
    private static let kitInstructions = """
        You write short listings for second-hand household items.

        Write two or three plain sentences. Say what the thing is, what \
        it is made of if that is known, and its condition honestly, \
        including any damage you are told about. No exclamation marks, \
        no "stunning", no "rare", no invented history, no shipping or \
        payment terms.

        Never invent a fact you were not given - especially an age, a \
        maker or a material. If you were not told, leave it out.
        """

    /// A draft description from the fields she has already typed.
    ///
    /// Text in, text out, which is the iOS 26 half of the framework and
    /// works without an image. It is also the low-risk half: she reads
    /// the paragraph before it goes anywhere, so a bad sentence is
    /// visible in a way a wrong `era` in a form field is not.
    static func draftListing(for item: Item) async throws -> String {
        let session = LanguageModelSession(instructions: kitInstructions)
        do {
            let response = try await session.respond(to: prompt(for: item))
            return tidy(response.content
                .trimmingCharacters(in: .whitespacesAndNewlines))
        } catch {
            throw refusal(error)
        }
    }

    /// The safety classifier fires on ordinary second-hand furniture
    /// copy, and it is not always obvious why - the phrasing that tripped
    /// it here was a description of the *seller*, not of anything for
    /// sale. So a refusal has to arrive as a sentence rather than as a
    /// raw error, and it must never read as her having done something
    /// wrong.
    static func refusal(_ error: Error) -> Error {
        if case LanguageModelSession.GenerationError.guardrailViolation =
            error {
            return Refusal(message:
                "The on-device model declined to write this one. It does "
                + "that occasionally on wording it cannot place, and it is "
                + "not about what you typed. Write the description "
                + "yourself, or change a word and try again.")
        }
        return error
    }

    struct Refusal: LocalizedError {
        let message: String
        var errorDescription: String? { message }
    }

    /// Built here rather than inline so it can be read - and tested -
    /// without a model.
    ///
    /// **A missing field is named, not omitted**, and that is the
    /// opposite of what this did first. Leaving `Condition` out
    /// altogether looked obviously right - a blank label invites the
    /// model to fill it - and it is wrong: asked about a cast iron
    /// skillet with nothing said about its condition, three runs out of
    /// three invented damage ("light rust on the bottom edge", "handle
    /// is slightly bent", "minor chipping on one side"). Naming the gap
    /// stopped it dead, three runs out of three.
    ///
    /// That matters more here than in most places a model is wrong: an
    /// invented chip is a false statement about an object a buyer is
    /// going to open a box and look at.
    static func prompt(for item: Item) -> String {
        var lines = ["Write the listing for this item."]
        func add(_ label: String, _ value: String, sayWhenMissing: Bool) {
            let trimmed = value.trimmingCharacters(in: .whitespaces)
            if !trimmed.isEmpty {
                lines.append("\(label): \(trimmed)")
            } else if sayWhenMissing {
                lines.append("\(label): not stated")
            }
        }
        add("Item", item.title, sayWhenMissing: false)
        // The two it invents. Price it does not, and a "not stated"
        // price would only invite it to suggest one.
        add("Era", item.era, sayWhenMissing: true)
        add("Condition", item.condition, sayWhenMissing: true)
        add("Asking price", item.asking.isEmpty ? "" : "$" + item.asking,
            sayWhenMissing: false)
        return lines.joined(separator: "\n")
    }

    /// Take the model's acknowledgement of a gap back out.
    ///
    /// Naming the gap is what stops the invention, and the price of it is
    /// that the draft then says "Condition: not stated" - which she would
    /// delete by hand every single time. Asked to stay quiet about it
    /// instead, the model ignores the instruction and echoes the line
    /// verbatim; a 3B model does not reliably omit on request. So it is
    /// removed here, where it is deterministic and testable rather than
    /// a thing we hope the model does.
    static func tidy(_ text: String) -> String {
        let dead = ["not stated", "not specified", "no condition details",
                    "condition unknown", "unknown condition", "not provided",
                    "no details about"]
        let kept = text
            .replacingOccurrences(of: "\n", with: " ")
            .split(separator: ".", omittingEmptySubsequences: true)
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { sentence in
                let lower = sentence.lowercased()
                return !dead.contains { lower.contains($0) }
            }
            .filter { !$0.isEmpty }
        return kept.isEmpty ? "" : kept.joined(separator: ". ") + "."
    }

    // MARK: - suggestions from a photograph

    private static let lookInstructions = """
        You are looking at a photograph of a single second-hand object \
        that someone is about to list for sale.

        Describe only what is visible. Do not guess an age, a maker or a \
        value from style alone - leave a field empty rather than fill it \
        with something plausible. Empty is a useful answer and a wrong \
        one is not.
        """

    /// Title, era, condition and category off a photograph.
    ///
    /// **iOS 27**, not 26: the text model arrived a version before it
    /// could be shown a picture. The caller gates on this, and the
    /// listing kit above deliberately does not depend on it.
    /// Whether this *build* can show the model a picture at all.
    ///
    /// Two conditions of different kinds. `iOS 27` is a runtime question
    /// about the phone; the compiler check is about the SDK the binary
    /// was built against - `Attachment` is not in the iOS 26 SDK, so
    /// code naming it does not compile there, and an `@available`
    /// annotation cannot rescue a symbol that is absent from the
    /// headers.
    ///
    /// That matters because the release runner has whatever Xcode ships
    /// and the image API is still in a beta. Without this the whole app
    /// fails to build on CI - which is what happened the day CI arrived,
    /// on `main` rather than on anybody's branch.
    static var canSeePictures: Bool {
        #if compiler(>=6.4)
        if #available(iOS 27.0, *) { return true }
        return false
        #else
        return false
        #endif
    }

    @available(iOS 27.0, *)
    static func suggestions(from image: CGImage,
                            typedTitle: String = "") async throws -> Suggested {
        #if compiler(>=6.4)
        let session = LanguageModelSession(instructions: lookInstructions)
        let hint = typedTitle.trimmingCharacters(in: .whitespaces)
        do {
        let response = try await session.respond(generating: Suggested.self) {
            Attachment(image)
            hint.isEmpty
                ? "Describe this object for a listing."
                : "Describe this object for a listing. She has called it "
                  + "\"\(hint)\" - correct that only if the photograph "
                  + "plainly disagrees."
        }
        return response.content
        } catch {
            throw refusal(error)
        }
        #else
        // Built against an SDK without the image API. The signature
        // stays, so callers need no compiler guards of their own, and
        // the refusal distinguishes the two reasons: a phone that is too
        // old reads very differently from a build made before the SDK
        // shipped.
        throw Refusal(message:
            "This build cannot show the model a picture - it was made "
            + "with an SDK that predates that. The written draft still "
            + "works.")
        #endif
    }
}
