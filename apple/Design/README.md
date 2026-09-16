# Kyra app icon

The app icon shares the cyan sphere and open orbit of Kyra's presence. It uses original native drawing commands, with no third-party artwork or extra dependencies.

From the repository root on macOS:

```sh
swift apple/Design/generate_app_icon.swift
```

This regenerates the three 1024 x 1024 sRGB PNG layers in `KyraVision/KyraVision/Assets.xcassets/AppIcon.solidimagestack` beneath `apple/`, plus `KyraIcon-preview.png` beside this file. The square preview is not used by the app. visionOS applies the circular mask to the full opaque background, with the sphere and open orbit on transparent layers above it.

Apple's guidance: [app icon design](https://developer.apple.com/design/human-interface-guidelines/app-icons) and [asset catalog configuration](https://developer.apple.com/documentation/xcode/configuring-your-app-icon).
