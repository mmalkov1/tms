plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

// Реліз підписуємо ключем із keystore. У CI він приїжджає з GitHub Secrets.
// Без підпису оновлення «поверх» не встановиться: Android вимагає той самий ключ.
val ksFile    = System.getenv("KEYSTORE_FILE") ?: "keystore.jks"
val ksPass    = System.getenv("KEYSTORE_PASSWORD") ?: ""
val ksAlias   = System.getenv("KEY_ALIAS") ?: "tms"
val ksKeyPass = System.getenv("KEY_PASSWORD") ?: ksPass

android {
    namespace = "ua.kult.tmsdriver"
    compileSdk = 34

    defaultConfig {
        applicationId = "ua.kult.tmsdriver"
        minSdk = 26            // Android 8.0+
        targetSdk = 34
        versionCode = 9        // ПІДНІМАТИ на кожну нову збірку APK
        versionName = "1.8"
    }

    signingConfigs {
        create("release") {
            val f = rootProject.file(ksFile)
            if (f.exists() && ksPass.isNotBlank()) {
                storeFile = f
                storePassword = ksPass
                keyAlias = ksAlias
                keyPassword = ksKeyPass
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            val f = rootProject.file(ksFile)
            if (f.exists() && ksPass.isNotBlank())
                signingConfig = signingConfigs.getByName("release")
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("com.google.android.gms:play-services-location:21.3.0")
    // v37: сканер QR від Play Services — без дозволу на камеру і власного UI
    implementation("com.google.android.gms:play-services-code-scanner:16.1.0")
}
