
import cv2
import os
import csv
import tkinter as tk

from tkinter import filedialog
from ultralytics import YOLO
from datetime import datetime

# ==========================================
# 1. CONFIGURATION
# ==========================================

MODEL_NAME = "yolov8n.pt"

CONFIDENCE_THRESHOLD = 0.35

OUTPUT_FOLDER = "processed_images"

# COCO class ID 0 = person
PERSON_CLASS_ID = 0

# ==========================================
# 2. LOAD YOLO MODEL
# ==========================================

print("Loading YOLO model...")

model = YOLO(MODEL_NAME)

print("YOLO model loaded successfully!")

# Create output directory
os.makedirs(OUTPUT_FOLDER, exist_ok=True)


# ==========================================
# 3. SELECT IMAGE FILES
# ==========================================

root = tk.Tk()
root.withdraw()

image_files = filedialog.askopenfilenames(
    title="Select Passenger Images",
    filetypes=[
        ("Image Files", "*.jpg *.jpeg *.png *.bmp"),
        ("All Files", "*.*")
    ]
)

if not image_files:
    print("No images selected.")
    root.destroy()
    exit()

# ==========================================
# 4. CSV REPORT
# ==========================================

csv_path = os.path.join(
    OUTPUT_FOLDER,
    "passenger_count_report.csv"
)

results_data = []

# ==========================================
# 5. PROCESS EACH IMAGE
# ==========================================

for image_path in image_files:

    print("\n--------------------------------")
    print("Processing:", image_path)

    # Read image
    image = cv2.imread(image_path)

    if image is None:
        print("Error: Cannot read image.")
        continue

    # YOLO inference
    results = model.predict(
        source=image,
        conf=CONFIDENCE_THRESHOLD,
        verbose=False
    )

    result = results[0]

    # Count detected people
    passenger_count = 0

    # Draw bounding boxes
    for box in result.boxes:

        class_id = int(box.cls[0])

        confidence = float(box.conf[0])

        # Detect only people
        if class_id == PERSON_CLASS_ID:

            passenger_count += 1

            x1, y1, x2, y2 = map(
                int, box.xyxy[0].tolist()
            )

            # Draw rectangle
            cv2.rectangle(
                image,
                (x1, y1),
                (x2, y2),
                (0, 255, 0),
                2
            )

            # Display confidence
            label = f"Person {confidence:.2f}"

            cv2.putText(
                image,
                label,
                (x1, max(y1 - 10, 20)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.5,
                (0, 255, 0),
                2
            )

    # ======================================
    # 6. DISPLAY TOTAL COUNT
    # ======================================

    count_text = f"Passengers: {passenger_count}"

    cv2.rectangle(
        image,
        (10, 10),
        (330, 60),
        (0, 0, 0),
        -1
    )

    cv2.putText(
        image,
        count_text,
        (20, 45),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.9,
        (0, 255, 255),
        2
    )

    # ======================================
    # 7. SAVE PROCESSED IMAGE
    # ======================================

    filename = os.path.basename(image_path)

    output_path = os.path.join(
        OUTPUT_FOLDER,
        "detected_" + filename
    )

    cv2.imwrite(output_path, image)

    # ======================================
    # 8. DISPLAY RESULTS
    # ======================================

    print("Passenger count:", passenger_count)
    print("Saved:", output_path)

    results_data.append({
        "Image": filename,
        "Passenger_Count": passenger_count,
        "Timestamp": datetime.now().isoformat(
            timespec="seconds"
        )
    })

    cv2.imshow("Passenger Counting System", image)

    print("Press any key to process next image...")

    cv2.waitKey(0)

# ==========================================
# 9. SAVE CSV REPORT
# ==========================================

if results_data:

    with open(
        csv_path,
        "w",
        newline="",
        encoding="utf-8"
    ) as file:

        writer = csv.DictWriter(
            file,
            fieldnames=[
                "Image",
                "Passenger_Count",
                "Timestamp"
            ]
        )

        writer.writeheader()
        writer.writerows(results_data)

    print("\n================================")
    print("PROCESSING COMPLETED")
    print("================================")

    print("Total images:", len(results_data))
    print("Report saved:", csv_path)

cv2.destroyAllWindows()
root.destroy()
