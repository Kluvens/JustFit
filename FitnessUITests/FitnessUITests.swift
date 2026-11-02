import XCTest

final class FitnessUITests: XCTestCase {
    func testSyntheticWorkoutSync() throws {
        let bundle = Bundle(for: FitnessUITests.self)
        guard let testPort = bundle.object(forInfoDictionaryKey: "FitnessTestPort") as? String,
              let testFingerprint = bundle.object(forInfoDictionaryKey: "FitnessTestFingerprint") as? String,
              Int(testPort) != nil, testFingerprint.count == 64 else { XCTFail("Missing isolated test endpoint"); return }
        let app = XCUIApplication()
        app.launchArguments = ["--ui-testing", "--ui-reset"]
        app.launch()
        XCTAssertTrue(app.buttons["练胸"].waitForExistence(timeout: 10))
        app.buttons["练胸"].tap()
        let screen = XCTAttachment(screenshot: app.screenshot())
        screen.name = "动作列表图示"
        screen.lifetime = .keepAlways
        add(screen)
        app.buttons.matching(NSPredicate(format: "label CONTAINS %@", "杠铃卧推")).firstMatch.tap()
        let weight = app.textFields["重量 kg（可空）"]
        XCTAssertTrue(weight.waitForExistence(timeout: 5))
        weight.tap()
        weight.typeText("40")
        let reps = app.textFields["次数"]
        reps.tap()
        reps.typeText("10")
        // Reopen without the test reset: unfinished input must survive process exit.
        app.terminate()
        app.launchArguments = ["--ui-testing"]
        app.launch()
        let draft = app.buttons.matching(NSPredicate(format: "label CONTAINS %@", "正在记录")).firstMatch
        XCTAssertTrue(draft.waitForExistence(timeout: 5))
        draft.tap()
        XCTAssertEqual(Double(app.textFields["重量 kg（可空）"].value as? String ?? ""), 40)
        XCTAssertEqual(app.textFields["次数"].value as? String, "10")
        app.switches["完成"].tap()
        app.buttons["完成训练"].tap()
        app.navigationBars.buttons.element(boundBy: 0).tap()
        app.tabBars.buttons["同步"].tap()
        pairMac(app, port: testPort, fingerprint: testFingerprint, secret: "synthetic-test-only-012345678901234567890123456789")
        let sync = app.buttons["与 Mac 同步"]
        XCTAssertTrue(sync.waitForExistence(timeout: 5))
        XCTAssertTrue(sync.isEnabled, app.debugDescription)
        guard sync.isEnabled else { return }
        sync.tap()
        let accepted = app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", "已收录 1 / 1")).firstMatch
        XCTAssertTrue(accepted.waitForExistence(timeout: 15))
        app.swipeUp()
        XCTAssertTrue(app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", "待生成或更新 1 个月")).firstMatch.waitForExistence(timeout: 5))
    }

    func testRecoveryOnNewDevice() throws {
        let bundle = Bundle(for: FitnessUITests.self)
        guard let testPort = bundle.object(forInfoDictionaryKey: "FitnessTestPort") as? String,
              let testFingerprint = bundle.object(forInfoDictionaryKey: "FitnessTestFingerprint") as? String,
              Int(testPort) != nil, testFingerprint.count == 64 else { XCTFail("Missing isolated test endpoint"); return }
        let app = XCUIApplication()
        app.launchArguments = ["--ui-testing", "--ui-reset"]
        app.launch()
        XCTAssertTrue(app.tabBars.buttons["同步"].waitForExistence(timeout: 10))
        app.tabBars.buttons["同步"].tap()
        pairMac(app, port: testPort, fingerprint: testFingerprint, secret: "synthetic-replacement-only-0123456789012345678901")
        let recover = app.buttons["从 Mac 恢复"]
        app.swipeUp()
        XCTAssertTrue(recover.waitForExistence(timeout: 5))
        guard recover.isEnabled else { XCTFail(app.debugDescription); return }
        recover.tap()
        app.swipeDown()
        let restored = app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", "已恢复 1 次训练")).firstMatch
        XCTAssertTrue(restored.waitForExistence(timeout: 15))
        app.tabBars.buttons["历史"].tap()
        let record = app.buttons.matching(NSPredicate(format: "label CONTAINS %@", "1 个动作")).firstMatch
        XCTAssertTrue(record.waitForExistence(timeout: 5))
        record.tap()
        XCTAssertEqual(Double(app.textFields["重量 kg（可空）"].value as? String ?? ""), 40)
        XCTAssertEqual(app.textFields["次数"].value as? String, "10")
        let weight = app.textFields["重量 kg（可空）"]
        weight.tap(); weight.typeText("5")
        app.tabBars.buttons["同步"].tap()
        let sync = app.buttons["与 Mac 同步"]
        XCTAssertTrue(sync.waitForExistence(timeout: 5))
        sync.tap()
        XCTAssertTrue(app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", "已收录 1 / 1")).firstMatch.waitForExistence(timeout: 15))
    }

    func testStaleRecordShowsReasonAndIsNotAccepted() throws {
        let app = XCUIApplication()
        app.launchArguments = ["--ui-testing"]
        app.launch()
        XCTAssertTrue(app.tabBars.buttons["同步"].waitForExistence(timeout: 10))
        app.tabBars.buttons["同步"].tap()
        app.buttons["与 Mac 同步"].tap()
        XCTAssertTrue(app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", "Mac 已有更新版本")).firstMatch.waitForExistence(timeout: 15))
        XCTAssertTrue(app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", "已收录 0 / 1")).firstMatch.waitForExistence(timeout: 5))
    }

    func testMissingReceiptIsNotReportedAsSuccessfulSync() throws {
        let app = XCUIApplication()
        app.launchArguments = ["--ui-testing"]
        app.launch()
        XCTAssertTrue(app.tabBars.buttons["同步"].waitForExistence(timeout: 10))
        app.tabBars.buttons["同步"].tap()
        app.buttons["与 Mac 同步"].tap()
        XCTAssertTrue(app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", "Mac 返回的数据无效")).firstMatch.waitForExistence(timeout: 15))
    }

    func testReceiptManifestMismatchIsNotAccepted() throws {
        let app = XCUIApplication()
        app.launchArguments = ["--ui-testing"]
        app.launch()
        XCTAssertTrue(app.tabBars.buttons["同步"].waitForExistence(timeout: 10))
        app.tabBars.buttons["同步"].tap()
        app.buttons["与 Mac 同步"].tap()
        XCTAssertTrue(app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", "回执与 Mac 清单不一致")).firstMatch.waitForExistence(timeout: 15))
        app.swipeUp()
        XCTAssertTrue(app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", "已收录 0 / 1")).firstMatch.waitForExistence(timeout: 5))
    }

    func testDeleteWorkoutAndSync() throws {
        let app = XCUIApplication()
        app.launchArguments = ["--ui-testing"]
        app.launch()
        XCTAssertTrue(app.buttons["练腿"].waitForExistence(timeout: 10))
        app.buttons["练腿"].tap()
        app.buttons.matching(NSPredicate(format: "label CONTAINS %@", "徒手深蹲")).firstMatch.tap()
        let reps = app.textFields["次数"]
        XCTAssertTrue(reps.waitForExistence(timeout: 5))
        reps.tap(); reps.typeText("12")
        app.switches["完成"].tap()
        app.buttons["完成训练"].tap()
        app.tabBars.buttons["同步"].tap()
        app.buttons["与 Mac 同步"].tap()
        XCTAssertTrue(app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", "已收录 1 / 2")).firstMatch.waitForExistence(timeout: 15))
        app.tabBars.buttons["历史"].tap()
        let newest = app.buttons.matching(NSPredicate(format: "label CONTAINS %@", "1 个动作")).firstMatch
        XCTAssertTrue(newest.waitForExistence(timeout: 5))
        newest.swipeLeft()
        let delete = app.buttons["Delete"].exists ? app.buttons["Delete"] : app.buttons["删除"]
        XCTAssertTrue(delete.waitForExistence(timeout: 5))
        delete.tap()
        XCTAssertTrue(app.buttons["删除训练"].waitForExistence(timeout: 5))
        app.buttons["取消"].tap()
        XCTAssertTrue(newest.exists)
        newest.swipeLeft()
        let deleteAgain = app.buttons["Delete"].exists ? app.buttons["Delete"] : app.buttons["删除"]
        XCTAssertTrue(deleteAgain.waitForExistence(timeout: 5))
        deleteAgain.tap()
        app.buttons["删除训练"].tap()
        app.tabBars.buttons["同步"].tap()
        app.buttons["与 Mac 同步"].tap()
        XCTAssertTrue(app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", "已收录 0 / 1")).firstMatch.waitForExistence(timeout: 15))
    }

    func testRemoveExercise() throws {
        let app = XCUIApplication()
        app.launchArguments = ["--ui-testing", "--ui-reset"]
        app.launch()
        XCTAssertTrue(app.buttons["练胸"].waitForExistence(timeout: 10))
        app.buttons["练胸"].tap()
        app.buttons.matching(NSPredicate(format: "label CONTAINS %@", "杠铃卧推")).firstMatch.tap()
        let remove = app.buttons["移除这个动作"]
        XCTAssertTrue(remove.waitForExistence(timeout: 5))
        remove.tap()
        app.buttons["移除动作"].tap()
        XCTAssertTrue(app.textFields["重量 kg（可空）"].waitForNonExistence(timeout: 5))
        app.terminate()
        app.launchArguments = ["--ui-testing"]
        app.launch()
        let draft = app.buttons.matching(NSPredicate(format: "label CONTAINS %@", "正在记录")).firstMatch
        XCTAssertTrue(draft.waitForExistence(timeout: 5))
        draft.tap()
        XCTAssertFalse(app.textFields["重量 kg（可空）"].exists)
    }

    private func pairMac(_ app: XCUIApplication, port testPort: String, fingerprint testFingerprint: String, secret testSecret: String) {
        app.buttons["手动输入配对信息"].tap()
        let host = app.textFields["Mac 本地地址"]
        XCTAssertTrue(host.waitForExistence(timeout: 5))
        host.tap(); host.typeText("127.0.0.1")
        let port = app.textFields["端口"]
        port.tap(); port.typeText(testPort)
        let fingerprint = app.textFields["证书 SHA-256 指纹"]
        fingerprint.tap(); fingerprint.typeText(testFingerprint)
        let secret = app.secureTextFields["配对密钥"]
        secret.tap(); secret.typeText(testSecret)
        app.buttons["保存手动配对信息"].tap()
    }
}
