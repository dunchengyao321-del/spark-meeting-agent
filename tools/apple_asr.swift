// apple_asr — local transcription using the macOS Speech framework.
// Usage: apple_asr [--auth] [--local] [--hotwords <file>] <file.wav> [locale]
//   --auth      only request/report authorization status
//   --local     force on-device recognition (offline, lower accuracy)
//   --hotwords  text file with one phrase per line, fed to the recognizer as
//               contextualStrings (biases domain vocabulary like 唤醒名/项目名)
// Default: Apple's speech service first (much better zh-CN accuracy),
// then on-device as offline fallback.
// Exit codes: 0 ok · 1 recognition error · 2 not authorized · 3 unavailable
//             4 timeout · 5 authorization dialog shown · 64 bad usage
import Foundation
import Speech

let args = Array(CommandLine.arguments.dropFirst())
let authOnly = args.contains("--auth")
let forceLocal = args.contains("--local")
var hotwords: [String] = []
var hotwordsPath: String?
if let hwIdx = args.firstIndex(of: "--hotwords"), hwIdx + 1 < args.count {
    hotwordsPath = args[hwIdx + 1]
    if let content = try? String(contentsOfFile: hotwordsPath!, encoding: .utf8) {
        hotwords = content.split(separator: "\n").map {
            $0.trimmingCharacters(in: .whitespaces)
        }.filter { !$0.isEmpty }
    }
}
let rest = args.filter { !$0.hasPrefix("--") && $0 != hotwordsPath }

func waitForAuth() -> Int {
    let status = SFSpeechRecognizer.authorizationStatus()
    if status == .authorized { return 0 }
    if status != .notDetermined { return 2 }
    var settled = false
    var granted = false
    SFSpeechRecognizer.requestAuthorization { s in
        granted = (s == .authorized)
        settled = true
    }
    let promptShownAt = Date().addingTimeInterval(3)
    while !settled && Date() < promptShownAt {
        RunLoop.main.run(mode: .default, before: Date().addingTimeInterval(0.1))
    }
    if settled { return granted ? 0 : 2 }
    // Dialog is on screen: wait for the user to click.
    let deadline = Date().addingTimeInterval(90)
    while !settled && Date() < deadline {
        RunLoop.main.run(mode: .default, before: Date().addingTimeInterval(0.1))
    }
    return granted ? 0 : 2
}

let authRc = waitForAuth()
if authOnly {
    if authRc == 0 { print("authorized") }
    exit(Int32(authRc == 0 ? 0 : (SFSpeechRecognizer.authorizationStatus() == .notDetermined ? 5 : authRc)))
}

guard authRc == 0 else {
    FileHandle.standardError.write(SFSpeechRecognizer.authorizationStatus() == .notDetermined
        ? "prompt_shown\n".data(using: .utf8)! : "auth_denied\n".data(using: .utf8)!)
    exit(SFSpeechRecognizer.authorizationStatus() == .notDetermined ? 5 : 2)
}
guard rest.count >= 1 else {
    FileHandle.standardError.write("usage: apple_asr [--auth] [--local] [--hotwords <file>] <file.wav> [locale]\n".data(using: .utf8)!)
    exit(64)
}
let audioPath = rest[0]
let localeId = rest.count > 1 ? rest[1] : "zh-CN"

guard let recognizer = SFSpeechRecognizer(locale: Locale(identifier: localeId)),
      recognizer.isAvailable else {
    FileHandle.standardError.write("recognizer_unavailable\n".data(using: .utf8)!)
    exit(3)
}

func recognize(onDevice: Bool) -> (text: String?, error: String?) {
    let request = SFSpeechURLRecognitionRequest(url: URL(fileURLWithPath: audioPath))
    request.shouldReportPartialResults = false
    if onDevice { request.requiresOnDeviceRecognition = true }
    if !hotwords.isEmpty { request.contextualStrings = hotwords }
    // 口述任务提示：减少标点/断句层面的误判
    request.taskHint = .dictation
    var finished = false
    var transcript: String?
    var failure: String?
    let task = recognizer.recognitionTask(with: request) { result, error in
        if let result = result, result.isFinal {
            transcript = result.bestTranscription.formattedString
            finished = true
        }
        if let error = error {
            failure = error.localizedDescription
            finished = true
        }
    }
    let deadline = Date().addingTimeInterval(45)
    while !finished && Date() < deadline {
        RunLoop.main.run(mode: .default, before: Date().addingTimeInterval(0.05))
    }
    if !finished {
        task.cancel()
        return (nil, "timeout")
    }
    return (transcript, failure)
}

var attempts: [Bool] = []
if forceLocal {
    attempts = recognizer.supportsOnDeviceRecognition ? [true] : []
} else {
    attempts.append(false)  // 云端优先：zh-CN 精度显著好于设备端
    if recognizer.supportsOnDeviceRecognition { attempts.append(true) }  // 离线兜底
}

var lastError = "no_attempt"
for onDevice in attempts {
    let outcome = recognize(onDevice: onDevice)
    if let text = outcome.text {
        print(text)
        exit(0)
    }
    lastError = outcome.error ?? "unknown"
    FileHandle.standardError.write("attempt(onDevice=\(onDevice)) failed: \(lastError)\n".data(using: .utf8)!)
}
FileHandle.standardError.write("error: \(lastError)\n".data(using: .utf8)!)
exit(lastError == "timeout" ? 4 : 1)
