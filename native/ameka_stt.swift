// ameka-stt — free, on-device speech recognition using Apple's Speech framework.
// Usage: ameka-stt <audio-file> [--server-ok]
// Prints the transcript on stdout. No API key, no network unless --server-ok
// is passed and the on-device model is unavailable.
import Foundation
import Speech

let args = CommandLine.arguments
guard args.count > 1 else {
    FileHandle.standardError.write("usage: ameka-stt <audio-file> [--server-ok]\n".data(using: .utf8)!)
    exit(2)
}
let url = URL(fileURLWithPath: args[1])
let serverOK = args.contains("--server-ok")
// When launched through LaunchServices (open -a) stdout goes nowhere, so the
// transcript is written to this file instead.
let outPath: String? = args.count > 2 && !args[2].hasPrefix("--") ? args[2] : nil

func emit(_ text: String) {
    if let outPath = outPath {
        try? text.write(toFile: outPath, atomically: true, encoding: .utf8)
    } else {
        print(text)
    }
}

func fail(_ code: Int32, _ message: String) -> Never {
    FileHandle.standardError.write((message + "\n").data(using: .utf8)!)
    if let outPath = outPath { try? ("!" + message).write(toFile: outPath, atomically: true, encoding: .utf8) }
    exit(code)
}

// Authorisation is remembered by macOS after the first grant.
var granted = false
let authSem = DispatchSemaphore(value: 0)
SFSpeechRecognizer.requestAuthorization { status in
    granted = (status == .authorized)
    authSem.signal()
}
_ = authSem.wait(timeout: .now() + 60)
guard granted else { fail(3, "speech-recognition-not-authorised") }

guard let recognizer = SFSpeechRecognizer(locale: Locale(identifier: "en-US")) else {
    fail(4, "no-recognizer-for-locale")
}
guard recognizer.isAvailable else { fail(5, "recognizer-unavailable") }

let request = SFSpeechURLRecognitionRequest(url: url)
request.shouldReportPartialResults = false
request.addsPunctuation = true
if recognizer.supportsOnDeviceRecognition {
    request.requiresOnDeviceRecognition = true
} else if !serverOK {
    fail(6, "on-device-model-unavailable")
}

var transcript = ""
var failure: Error?
let done = DispatchSemaphore(value: 0)
let task = recognizer.recognitionTask(with: request) { result, error in
    if let error = error { failure = error; done.signal(); return }
    guard let result = result else { return }
    if result.isFinal {
        transcript = result.bestTranscription.formattedString
        done.signal()
    }
}
if done.wait(timeout: .now() + 20) == .timedOut { task.cancel() }
if transcript.isEmpty, let failure = failure {
    fail(7, "recognition-failed: \(failure.localizedDescription)")
}
emit(transcript.trimmingCharacters(in: .whitespacesAndNewlines))
