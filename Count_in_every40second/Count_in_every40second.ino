
#include <Arduino.h>
#include "USB_STREAM.h"
#include "esp_log.h"

#include <WiFi.h>
#include <HTTPClient.h>

#include <string.h>
#include <stdlib.h>

#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"

// ==========================================
// 1. WIFI CONFIGURATION
// ==========================================

const char* WIFI_SSID = "YOUR_WIFI_SSID";
const char* WIFI_PASSWORD = "YOUR_WIFI_PASSWORD";

// Set this to the address of the image receiver on the Raspberry Pi.
const char* SERVER_URL = "http://<RASPBERRY_PI_IP>:5000/upload";

// ==========================================
// 2. UPLOAD TIMING CONFIGURATION
// ==========================================

const unsigned long UPLOAD_INTERVAL = 40000;  // 40 seconds
const unsigned long RETRY_INTERVAL = 5000;    // Retry after 5 seconds

unsigned long nextUploadTime = 0;

// ==========================================
// 3. CAMERA CONFIGURATION
// ==========================================

#define BUFFER_SIZE (55 * 1024)

USB_STREAM *usbStream = nullptr;

uint8_t *transferBufferA = nullptr;
uint8_t *transferBufferB = nullptr;
uint8_t *frameBuffer = nullptr;

// Camera callback buffer
uint8_t *capturedImage = nullptr;

// Separate buffer used during HTTP upload
uint8_t *uploadImageBuffer = nullptr;

uint32_t capturedImageLength = 0;

volatile uint32_t frameCount = 0;

bool imageReady = false;

// Mutex protects the camera image while copying
SemaphoreHandle_t imageMutex = nullptr;

// ==========================================
// 4. MEMORY INFORMATION
// ==========================================

void printMemoryInformation()
{
    Serial.println();
    Serial.println("========== MEMORY INFORMATION ==========");

    Serial.print("Free heap: ");
    Serial.println(ESP.getFreeHeap());

    Serial.print("Largest free block: ");
    Serial.println(ESP.getMaxAllocHeap());

    Serial.print("Free PSRAM: ");
    Serial.println(ESP.getFreePsram());

    Serial.println("========================================");
}

// ==========================================
// 5. CAMERA BUFFER ALLOCATION
// ==========================================

bool allocateCameraBuffers()
{
    Serial.println("\nAllocating camera buffers...");

    printMemoryInformation();

    transferBufferA = (uint8_t*)malloc(BUFFER_SIZE);

    if (transferBufferA == nullptr)
    {
        Serial.println("ERROR: transferBufferA allocation failed!");
        return false;
    }

    Serial.println("transferBufferA allocated.");

    transferBufferB = (uint8_t*)malloc(BUFFER_SIZE);

    if (transferBufferB == nullptr)
    {
        Serial.println("ERROR: transferBufferB allocation failed!");
        return false;
    }

    Serial.println("transferBufferB allocated.");

    frameBuffer = (uint8_t*)malloc(BUFFER_SIZE);

    if (frameBuffer == nullptr)
    {
        Serial.println("ERROR: frameBuffer allocation failed!");
        return false;
    }

    Serial.println("frameBuffer allocated.");

    capturedImage = (uint8_t*)malloc(BUFFER_SIZE);

    if (capturedImage == nullptr)
    {
        Serial.println("ERROR: capturedImage allocation failed!");
        return false;
    }

    Serial.println("capturedImage allocated.");

    uploadImageBuffer = (uint8_t*)malloc(BUFFER_SIZE);

    if (uploadImageBuffer == nullptr)
    {
        Serial.println("ERROR: uploadImageBuffer allocation failed!");
        return false;
    }

    Serial.println("uploadImageBuffer allocated.");

    // Create mutex before starting the camera
    imageMutex = xSemaphoreCreateMutex();

    if (imageMutex == nullptr)
    {
        Serial.println("ERROR: Image mutex creation failed!");
        return false;
    }

    Serial.println("Image mutex created.");

    Serial.println("\nALL CAMERA BUFFERS ALLOCATED!");

    printMemoryInformation();

    return true;
}

// ==========================================
// 6. WIFI CONNECTION
// ==========================================

bool connectToWiFi()
{
    Serial.println("\n================================");
    Serial.println("CHECKING WIFI CONNECTION");
    Serial.println("================================");

    if (WiFi.status() == WL_CONNECTED)
    {
        Serial.println("Already connected!");

        Serial.print("ESP32 IP: ");
        Serial.println(WiFi.localIP());

        return true;
    }

    WiFi.mode(WIFI_STA);
    WiFi.setSleep(false);
    WiFi.setAutoReconnect(true);

    Serial.print("Connecting to: ");
    Serial.println(WIFI_SSID);

    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

    int attempts = 0;

    while (WiFi.status() != WL_CONNECTED && attempts < 60)
    {
        delay(500);

        Serial.print("Attempt ");
        Serial.print(attempts + 1);

        Serial.print(" | WiFi status: ");
        Serial.println(WiFi.status());

        attempts++;
    }

    if (WiFi.status() == WL_CONNECTED)
    {
        Serial.println("\nWIFI CONNECTED SUCCESSFULLY!");

        Serial.print("ESP32 IP: ");
        Serial.println(WiFi.localIP());

        Serial.print("Signal strength: ");
        Serial.println(WiFi.RSSI());

        Serial.println("================================");

        return true;
    }

    Serial.println("\nWIFI CONNECTION FAILED!");

    return false;
}

// ==========================================
// 7. USB CAMERA CALLBACK
// ==========================================

void cameraFrameCallback(uvc_frame_t *frame, void *user_ctx)
{
    if (frame == nullptr ||
        frame->data == nullptr ||
        frame->data_bytes < 4)
    {
        return;
    }

    frameCount++;

    uint32_t imageSize = frame->data_bytes;

    if (imageSize > BUFFER_SIZE)
    {
        return;
    }

    uint8_t *imageData = (uint8_t*)frame->data;

    // Check JPEG start marker
    bool jpegStart =
        imageData[0] == 0xFF &&
        imageData[1] == 0xD8;

    // Check JPEG end marker
    bool jpegEnd =
        imageData[imageSize - 2] == 0xFF &&
        imageData[imageSize - 1] == 0xD9;

    if (!jpegStart || !jpegEnd)
    {
        return;
    }

    // Update the latest captured image.
    // If the main loop is copying an image, skip this frame.
    if (xSemaphoreTake(imageMutex, 0) == pdTRUE)
    {
        memcpy(capturedImage, imageData, imageSize);

        capturedImageLength = imageSize;

        imageReady = true;

        xSemaphoreGive(imageMutex);
    }
}

// ==========================================
// 8. UPLOAD IMAGE TO RASPBERRY PI
// ==========================================

bool uploadImage(uint32_t imageSize)
{
    Serial.println("\n================================");
    Serial.println("UPLOADING IMAGE TO RASPBERRY PI");

    Serial.print("Image size: ");
    Serial.print(imageSize);
    Serial.println(" bytes");

    Serial.println("================================");

    // Check WiFi
    if (!connectToWiFi())
    {
        Serial.println("Cannot upload image: WiFi unavailable.");
        return false;
    }

    HTTPClient http;

    // Allow enough time for YOLO processing on Raspberry Pi
    http.setTimeout(90000);

    Serial.println("\nConnecting to Flask server...");

    Serial.print("Server URL: ");
    Serial.println(SERVER_URL);

    if (!http.begin(SERVER_URL))
    {
        Serial.println("HTTP initialization failed!");
        return false;
    }

    http.addHeader("Content-Type", "image/jpeg");

    Serial.println("\nSending JPEG image...");

    int httpResponseCode = http.POST(
        uploadImageBuffer,
        imageSize
    );

    Serial.print("HTTP Response Code: ");
    Serial.println(httpResponseCode);

    bool uploadSuccessful = false;

    if (httpResponseCode > 0)
    {
        String response = http.getString();

        Serial.println("\nRaspberry Pi Server Response:");
        Serial.println(response);

        if (httpResponseCode == 200)
        {
            uploadSuccessful = true;

            Serial.println("\n================================");
            Serial.println("IMAGE SUCCESSFULLY UPLOADED!");
            Serial.println("YOLO PROCESSING COMPLETED!");
            Serial.println("================================");
        }
        else
        {
            Serial.println("Server returned an error.");
        }
    }
    else
    {
        Serial.print("HTTP Upload Error: ");

        Serial.println(
            http.errorToString(httpResponseCode)
        );
    }

    http.end();

    Serial.println("\nUpload process finished.");

    return uploadSuccessful;
}

// ==========================================
// 9. CAPTURE A SNAPSHOT AND UPLOAD
// ==========================================

void processAndUploadLatestImage()
{
    uint32_t imageSize = 0;

    // Copy the latest complete camera image into
    // a separate buffer. The camera can continue
    // receiving frames during the HTTP upload.

    if (xSemaphoreTake(imageMutex, portMAX_DELAY) == pdTRUE)
    {
        if (imageReady && capturedImageLength > 0)
        {
            imageSize = capturedImageLength;

            memcpy(
                uploadImageBuffer,
                capturedImage,
                imageSize
            );
        }

        xSemaphoreGive(imageMutex);
    }

    if (imageSize == 0)
    {
        Serial.println("No valid image available yet.");
        return;
    }

    Serial.println("\n================================");
    Serial.println("NEW IMAGE READY FOR UPLOAD");
    Serial.println("================================");

    bool success = uploadImage(imageSize);

    if (success)
    {
        Serial.println("\nNext image will be uploaded in 40 seconds.");

        nextUploadTime = millis() + UPLOAD_INTERVAL;
    }
    else
    {
        Serial.println("\nUpload failed!");
        Serial.println("Retrying in 5 seconds.");

        nextUploadTime = millis() + RETRY_INTERVAL;
    }
}

// ==========================================
// 10. USB HARDWARE INITIALIZATION
// ==========================================

void initializeUSBHardware()
{
    Serial.println("\nSTEP 1: USB HARDWARE INITIALIZATION");

    pinMode(18, OUTPUT);
    digitalWrite(18, HIGH);

    pinMode(12, OUTPUT);
    digitalWrite(12, HIGH);

    pinMode(17, OUTPUT);
    digitalWrite(17, HIGH);

    delay(500);

    Serial.println("USB hardware GPIO initialization finished.");
}

// ==========================================
// 11. USB CAMERA INITIALIZATION
// ==========================================

bool initializeUSBCamera()
{
    Serial.println("\nSTEP 4: INITIALIZING USB CAMERA");

    usbStream = new USB_STREAM();

    if (usbStream == nullptr)
    {
        Serial.println("ERROR: USB_STREAM allocation failed!");
        return false;
    }

    Serial.println("USB_STREAM object created.");

    Serial.println("Configuring UVC camera...");

    usbStream->uvcConfiguration(
        320,
        240,
        FRAME_INTERVAL_FPS_5,
        BUFFER_SIZE,
        transferBufferA,
        transferBufferB,
        BUFFER_SIZE,
        frameBuffer
    );

    Serial.println("UVC configuration completed.");

    usbStream->uvcCamRegisterCb(
        cameraFrameCallback,
        nullptr
    );

    Serial.println("Camera callback registered.");

    Serial.println("Starting USB stream...");

    usbStream->start();

    Serial.println("USB stream start command executed.");

    Serial.println("Waiting for USB camera connection...");

    usbStream->connectWait(10000);

    Serial.println("Camera connection wait finished.");

    printMemoryInformation();

    return true;
}

// ==========================================
// 12. SETUP
// ==========================================

void setup()
{
    Serial.begin(115200);

    delay(2000);

    Serial.println("\n");
    Serial.println("========================================");
    Serial.println("ESP32-S3 USB CAMERA IMAGE UPLOAD");
    Serial.println("ESP32-S3 TO RASPBERRY PI");
    Serial.println("AUTOMATIC UPLOAD EVERY 40 SECONDS");
    Serial.println("========================================");

    esp_log_level_set("*", ESP_LOG_INFO);

    // STEP 1: USB HARDWARE
    initializeUSBHardware();

    // STEP 2: CAMERA BUFFERS
    Serial.println("\nSTEP 2: CAMERA BUFFER ALLOCATION");

    if (!allocateCameraBuffers())
    {
        Serial.println("CAMERA BUFFER ALLOCATION FAILED!");

        while (true)
        {
            delay(1000);
        }
    }

    // STEP 3: WIFI
    Serial.println("\nSTEP 3: WIFI INITIALIZATION");

    if (connectToWiFi())
    {
        Serial.println("WiFi initialization successful.");
    }
    else
    {
        Serial.println("Initial WiFi connection failed.");
        Serial.println("Will retry during upload.");
    }

    // STEP 4: USB CAMERA
    if (!initializeUSBCamera())
    {
        Serial.println("USB CAMERA INITIALIZATION FAILED!");

        while (true)
        {
            delay(1000);
        }
    }

    Serial.println("\n========================================");
    Serial.println("USB CAMERA INITIALIZATION FINISHED");
    Serial.println("========================================");

    Serial.println("\nWaiting for first JPEG image...");

    // First upload is allowed immediately
    nextUploadTime = 0;
}

// ==========================================
// 13. MAIN LOOP
// ==========================================

void loop()
{
    static unsigned long lastPrintTime = 0;

    unsigned long currentTime = millis();

    bool imageAvailable = false;

    // Check whether a valid image has been captured
    if (xSemaphoreTake(imageMutex, portMAX_DELAY) == pdTRUE)
    {
        imageAvailable = imageReady && capturedImageLength > 0;

        xSemaphoreGive(imageMutex);
    }

    // Upload immediately when the first image is ready.
    // After that, upload every 40 seconds.
    if (imageAvailable &&
        (int32_t)(currentTime - nextUploadTime) >= 0)
    {
        processAndUploadLatestImage();

        return;
    }

    // Camera status
    if (!imageAvailable &&
        currentTime - lastPrintTime >= 3000)
    {
        lastPrintTime = currentTime;

        Serial.print("Waiting for camera frame... Frames received: ");
        Serial.println(frameCount);
    }

    delay(10);
}
