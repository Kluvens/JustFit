# JustFit

JustFit is an in-development iPhone workout logging app. It records exercises and sets for chest, back and legs, and synchronizes records with a local macOS receiver over a paired, certificate-pinned connection.

The project contains a SwiftUI app, a local Python/SQLite receiver and synthetic tests. The optional narrative-summary indexing integration depends on a separate local Knowledge System environment, which is not part of this repository. No workout data, pairing credentials, databases or personal knowledge-base files are included.

This is a development prototype: simulator and automated tests have been run, but physical-device installation, a real workout and independent backup recovery have not yet been accepted. It does not integrate with Apple Health or Apple Watch.

Open `FitnessApp.xcodeproj` in Xcode to build the iPhone target. A signing team is required for installation on an iPhone. For local backend checks run `python3 -m unittest -v test_fitness_store test_fitness_service test_fitness_knowledge`. The knowledge-boundary integration tests require the separate local indexing environment.

Exercise illustrations are distributed under CC BY-SA 4.0; see `EXERCISE_ART_ATTRIBUTION.json` for individual creators, sources, license links and modifications. No license grant for the remaining project source is stated in this repository.
