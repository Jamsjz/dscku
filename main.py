import datetime
import io
import os
import shutil
import tempfile
import uuid
import zipfile
from pathlib import Path

import pytz
from dotenv import load_dotenv
from fasthtml.common import *
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
from fasthtml_hf import setup_hf_backup

# === CONFIGURATION ===
load_dotenv()

# Load Google Service Account Credentials from environment variables
credentials = service_account.Credentials.from_service_account_info(
    {
        "type": os.getenv("GOOGLE_TYPE"),
        "project_id": os.getenv("GOOGLE_PROJECT_ID"),
        "private_key_id": os.getenv("GOOGLE_PRIVATE_KEY_ID"),
        "private_key": os.getenv("GOOGLE_PRIVATE_KEY"),
        "client_email": os.getenv("GOOGLE_CLIENT_EMAIL"),
        "client_id": os.getenv("GOOGLE_CLIENT_ID"),
        "auth_uri": os.getenv("GOOGLE_AUTH_URI"),
        "token_uri": os.getenv("GOOGLE_TOKEN_URI"),
        "auth_provider_x509_cert_url": os.getenv("GOOGLE_AUTH_PROVIDER_X509_CERT_URL"),
        "client_x509_cert_url": os.getenv("GOOGLE_CLIENT_X509_CERT_URL"),
        "universe_domain": os.getenv("GOOGLE_UNIVERSE_DOMAIN"),
    },
    scopes=["https://www.googleapis.com/auth/drive"],
)

DRIVE_SERVICE = build("drive", "v3", credentials=credentials)

ADMIN_PASSWD = os.environ.get("ADMIN_PASSWD")
if not ADMIN_PASSWD:
    raise Exception("ADMIN_PASSWD environment variable not set.")

DRIVE_PARENT_FOLDER_ID = os.environ.get("DRIVE_PARENT_FOLDER_ID")
if not DRIVE_PARENT_FOLDER_ID:
    raise Exception("DRIVE_PARENT_FOLDER_ID environment variable not set.")


# === ENHANCED FOLDER BOOTSTRAPPING ===
# === APPLICATION INITIALIZATION ===


def verify_or_create_folder(parent_id, folder_name):
    """Verify if a folder exists, create it if not, and return its ID"""
    try:
        # Check if folder already exists (with shared drive support)
        query = f"'{parent_id}' in parents and name='{folder_name}' and mimeType='application/vnd.google-apps.folder'"
        results = (
            DRIVE_SERVICE.files()
            .list(
                q=query,
                fields="files(id,name)",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )

        existing_folders = results.get("files", [])
        if existing_folders:
            return existing_folders[0]["id"]

        # Create the folder with shared drive support
        folder_metadata = {
            "name": folder_name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_id],
        }
        folder = (
            DRIVE_SERVICE.files()
            .create(body=folder_metadata, fields="id", supportsAllDrives=True)
            .execute()
        )

        print(f"Created folder: {folder_name} (ID: {folder.get('id')})")
        return folder.get("id")
    except Exception as e:
        print(f"Error creating folder {folder_name}: {str(e)}")
        raise


def test_drive_access():
    """Test Drive access with more detailed verification"""
    try:
        # Test 1: Basic API access
        DRIVE_SERVICE.files().list(pageSize=1).execute()

        # Test 2: Try to find our parent folder
        try:
            folder = (
                DRIVE_SERVICE.files()
                .get(
                    fileId=DRIVE_PARENT_FOLDER_ID,
                    fields="id,name",
                    supportsAllDrives=True,
                )
                .execute()
            )
            print(f"Found folder: {folder.get('name')} (ID: {folder.get('id')})")
            return True
        except Exception as e:
            print(f"Could not access specific folder: {str(e)}")
            return False

    except Exception as e:
        print(f"Drive API access failed completely: {str(e)}")
        return False


def initialize_drive():
    """Comprehensive Drive initialization with error handling"""
    try:
        # First test basic access
        if not test_drive_access():
            raise Exception("Basic Drive access test failed")

        # Try to get parent folder with different approaches
        parent = None
        try:
            parent = (
                DRIVE_SERVICE.files()
                .get(
                    fileId=DRIVE_PARENT_FOLDER_ID,
                    fields="id,name,mimeType",
                    supportsAllDrives=True,
                )
                .execute()
            )
        except Exception as e:
            # Try without supportsAllDrives for My Drive folders
            try:
                parent = (
                    DRIVE_SERVICE.files()
                    .get(fileId=DRIVE_PARENT_FOLDER_ID, fields="id,name,mimeType")
                    .execute()
                )
            except Exception:
                raise Exception(f"Could not access folder with either method: {str(e)}")

        if not parent:
            raise Exception("Parent folder not found with any access method")

        if parent.get("mimeType") != "application/vnd.google-apps.folder":
            raise Exception(f"ID {DRIVE_PARENT_FOLDER_ID} is not a folder")

        print(f"Using parent folder: {parent.get('name')} (ID: {parent.get('id')})")

        # Now create semester folders
        semester_folders = {}
        for semester_num in range(1, 9):
            folder_name = f"Semester_{semester_num}"
            try:
                folder_id = verify_or_create_folder(DRIVE_PARENT_FOLDER_ID, folder_name)
                semester_folders[folder_name] = folder_id
            except Exception as e:
                print(f"Warning: Could not create {folder_name}: {str(e)}")
                continue

        return semester_folders

    except Exception as e:
        print(f"Drive initialization failed: {str(e)}")
        # Print troubleshooting info
        print("\nTROUBLESHOOTING INFO:")
        print(f"1. Parent Folder ID: {DRIVE_PARENT_FOLDER_ID}")
        print(f"2. Service Account: {credentials.service_account_email}")
        print("3. Make sure the service account has Editor access to the folder")
        if "shared drive" in str(e).lower():
            print(
                "4. For Shared Drives, ensure the service account is added as a member"
            )
        raise


# === HELPER FUNCTIONS ===


def merge_zip_files(existing_path: Path, new_zip_path: Path):
    merged_temp = existing_path.parent / (existing_path.stem + "_merged.zip")
    with (
        zipfile.ZipFile(existing_path, "r") as old_zip,
        zipfile.ZipFile(new_zip_path, "r") as new_zip,
        zipfile.ZipFile(merged_temp, "w") as merged_zip,
    ):
        # Write files from the existing zip first
        for info in old_zip.infolist():
            merged_zip.writestr(info, old_zip.read(info.filename))
        # Add files from new zip (will replace duplicates)
        for info in new_zip.infolist():
            merged_zip.writestr(info, new_zip.read(info.filename))
    merged_temp.replace(existing_path)


def list_recent_uploads():
    """List recent uploads from Google Drive"""
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(hours=24)
    all_files = []

    for semester, folder_id in SEMESTER_FOLDER_IDS.items():
        results = (
            DRIVE_SERVICE.files()
            .list(
                q=f"'{folder_id}' in parents and modifiedTime > '{cutoff.isoformat()}Z'",
                spaces="drive",
                fields="files(id, name, modifiedTime)",
            )
            .execute()
        )

        for file in results.get("files", []):
            all_files.append(
                {
                    "semester": semester,
                    "filename": file["name"],
                    "filepath": file["id"],
                    "modified": datetime.datetime.fromisoformat(
                        file.get("modifiedTime", "").replace("Z", "+00:00")
                    ),
                }
            )

    return all_files


def list_files_in_semester(semester: str):
    """List files for a specific semester in Google Drive"""
    semester_folder_id = SEMESTER_FOLDER_IDS.get(semester)
    if not semester_folder_id:
        return []

    query = f"'{semester_folder_id}' in parents"
    results = (
        DRIVE_SERVICE.files()
        .list(q=query, spaces="drive", fields="files(id, name)")
        .execute()
    )
    a = [
        {"filename": file["name"], "filepath": file["id"]}
        for file in results.get("files", [])
    ]
    return a[::-1]


def upload_file_to_drive(file_bytes, filename, semester):
    """Upload a file to Google Drive, using a temporary file for MediaFileUpload."""
    try:
        semester_folder_id = SEMESTER_FOLDER_IDS.get(semester)
        if not semester_folder_id:
            raise ValueError(f"Folder not found for semester: {semester}")

        print(f"Uploading {filename} to {semester} (Folder ID: {semester_folder_id})")

        # Create a BytesIO object
        file_stream = io.BytesIO(file_bytes)

        # Verify zip contents (and ensure stream is reset)
        try:
            with zipfile.ZipFile(file_stream) as zip_ref:
                bad_file = zip_ref.testzip()
                if bad_file:
                    raise zipfile.BadZipFile(
                        f"Corrupt file in ZIP: {bad_file}"
                    )  # Use BadZipFile Exception
                print(f"Zip verified: {len(zip_ref.namelist())} files")

            file_stream.seek(0)  # Reset to beginning *after* zipfile operations
        except zipfile.BadZipFile as e:
            raise ValueError(f"Invalid ZIP file: {str(e)}")  # Raise ValueError
        except Exception as e:
            raise ValueError(f"Error during ZIP verification: {str(e)}")

        # Create a temporary file
        with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as tmp_file:
            tmp_file.write(file_stream.read())
            tmp_file_path = tmp_file.name

        # Create media upload
        media = MediaFileUpload(
            tmp_file_path, mimetype="application/zip", resumable=True
        )

        # Create file metadata
        file_metadata = {
            "name": filename,
            "parents": [semester_folder_id],
            "mimeType": "application/zip",
        }

        # Execute upload
        try:
            file = (
                DRIVE_SERVICE.files()
                .create(
                    body=file_metadata,
                    media_body=media,
                    fields="id",
                    supportsAllDrives=True,
                )
                .execute()
            )
        except Exception as e:
            print(f"Google Drive API Error: {str(e)}")
            raise  # Re-raise the exception to be caught outside
        finally:
            # Clean up the temporary file
            os.remove(tmp_file_path)

        print(f"Upload successful! File ID: {file.get('id')}")
        return file.get("id")

    except ValueError as ve:
        print(f"Validation Error: {str(ve)}")  # More specific message
        raise
    except Exception as e:
        print(f"General Upload Error: {str(e)}")
        raise


def download_file_from_drive(file_id):
    """Download a file from Google Drive"""
    request = DRIVE_SERVICE.files().get_media(fileId=file_id)
    file = io.BytesIO()
    downloader = MediaIoBaseDownload(file, request)
    done = False
    while not done:
        status, done = downloader.next_chunk()
    file.seek(0)
    return file.read()


def merge_zip_files_in_drive(existing_file_id, new_file_bytes):
    """Merge two zip files in Google Drive"""
    # Download existing file
    existing_file_content = download_file_from_drive(existing_file_id)

    # Create temporary files for merging
    with (
        tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as existing_temp,
        tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as new_temp,
        tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as merged_temp,
    ):
        existing_temp.write(existing_file_content)
        new_temp.write(new_file_bytes)
        existing_temp.close()
        new_temp.close()

        # Merge zip files
        merge_zip_files(Path(existing_temp.name), Path(new_temp.name))
        shutil.copy(Path(existing_temp.name), Path(merged_temp.name))

    # Read merged file and upload
    with open(merged_temp.name, "rb") as f:
        merged_content = f.read()

    # Clean up temporary files
    os.unlink(existing_temp.name)
    os.unlink(new_temp.name)
    os.unlink(merged_temp.name)

    # Replace existing file in Drive
    media = MediaFileUpload(
        io.BytesIO(merged_content), resumable=True, mimetype="application/zip"
    )
    DRIVE_SERVICE.files().update(fileId=existing_file_id, media_body=media).execute()


def delete_file_in_drive(file_id):
    """Delete a file in Google Drive"""
    try:
        DRIVE_SERVICE.files().delete(fileId=file_id).execute()
        return True
    except Exception:
        return False


# === FASTHTML SETUP ===


app, rt = fast_app(pico=True, live=False, debug=False)


@rt("/admin", methods=["GET"])
async def admin_page(req):
    """Admin page with upload form and recent files"""
    try:
        # Get the user's timezone from cookies or default to UTC
        user_timezone = req.cookies.get("timezone", "UTC")
        try:
            tz = pytz.timezone(user_timezone)
        except pytz.UnknownTimeZoneError:
            tz = pytz.UTC

        # Get recent uploads (last 24 hours in server time)
        cutoff = datetime.datetime.utcnow() - datetime.timedelta(hours=24)
        all_files = []

        for semester, folder_id in SEMESTER_FOLDER_IDS.items():
            try:
                results = (
                    DRIVE_SERVICE.files()
                    .list(
                        q=f"'{folder_id}' in parents and modifiedTime > '{cutoff.isoformat()}Z'",
                        fields="files(id, name, modifiedTime, mimeType, size)",
                        orderBy="modifiedTime desc",
                    )
                    .execute()
                )

                for file in results.get("files", []):
                    # Parse the UTC time from Google Drive
                    utc_time = datetime.datetime.fromisoformat(
                        file["modifiedTime"].replace("Z", "+00:00")
                    ).replace(tzinfo=pytz.UTC)

                    # Convert to user's local timezone
                    local_time = utc_time.astimezone(tz)

                    # Format the display time
                    formatted_time = local_time.strftime("%Y-%m-%d %H:%M")

                    # Convert size
                    size_mb = (
                        round(int(file.get("size", 0)) / (1024 * 1024), 2)
                        if "size" in file
                        else 0
                    )

                    all_files.append(
                        {
                            "semester": semester.replace("_", " "),
                            "filename": file["name"],
                            "file_id": file["id"],
                            "modified": formatted_time,
                            "size": f"{size_mb} MB" if size_mb > 0 else "N/A",
                            "type": file["mimeType"],
                        }
                    )
            except Exception as e:
                print(f"Error listing files for {semester}: {str(e)}")
                continue

        years = [str(datetime.datetime.utcnow().year - i) for i in range(6)]
        upload_form = Form(
            id="uploadForm",
            action="/admin/upload",
            enctype="multipart/form-data",
            method="post",
            onsubmit="return validateFile()",
        )(
            Label(
                "Semester",
                Select(
                    *[
                        Option(f"Semester {i}", value=f"Semester_{i}")
                        for i in range(1, 9)
                    ],
                    name="semester",
                    required=True,
                ),
            ),
            Label(
                "Batch Year",
                Select(
                    *[Option(y, value=y) for y in years],
                    name="batch_year",
                    required=True,
                ),
            ),
            Label(
                "File (zip only, max 50MB)",
                Input(
                    id="fileInput",
                    name="file",
                    type="file",
                    accept=".zip",
                    required=True,
                ),
            ),
            Label("Admin Password:", Input(type="password", name="password")),
            Div(id="errorMsg", style="color: red; margin: 10px 0;"),
            Button("Upload", type="submit"),
        )

        return Titled(
            "Admin Dashboard",
            Div(
                H3("Upload Rules:"),
                Ul(
                    Li("Only .zip files are allowed"),
                    Li("Maximum file size: 50MB"),
                    Li("Files can be deleted within 24 hours of upload"),
                    Li(
                        "Old files (up to 5 years) can be replaced by uploading new versions"
                    ),
                ),
                style="margin-bottom: 20px;",
            ),
            upload_form,
            Div(
                P(
                    f"Note: Files can only be deleted within 24 hours of upload (Displaying times in {tz.zone})",
                    style="color: #d9534f; font-weight: bold;",
                ),
                style="margin-bottom: 20px;",
            ),
            (
                Table(
                    Thead(
                        Tr(
                            Th("Semester"),
                            Th("Filename"),
                            Th("Modified"),
                            Th("Size"),
                            Th("Type"),
                            Th("Actions"),
                        )
                    ),
                    Tbody(
                        *[
                            Tr(
                                Td(file["semester"]),
                                Td(file["filename"]),
                                Td(file["modified"]),
                                Td(file["size"]),
                                Td(
                                    "ZIP Archive"
                                    if "zip" in file["type"]
                                    else file["type"]
                                ),
                                Td(
                                    A(
                                        "Delete",
                                        href=f"/admin/delete?file={file['file_id']}",
                                        style="color: #d9534f;",
                                    )
                                ),
                            )
                            for file in sorted(
                                all_files, key=lambda x: x["modified"], reverse=True
                            )
                        ]
                    ),
                )
                if all_files
                else P("No recent uploads found in the last 24 hours")
            ),
            A("Back to Home", href="/"),
            Script(
                """
                // Try to detect user's timezone
                try {
                    const userTimezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
                    document.cookie = `timezone=${userTimezone}; path=/; max-age=${60*60*24*365}`;
                } catch (e) {
                    console.log("Could not detect timezone", e);
                }

                function validateFile() {
                    const fileInput = document.getElementById('fileInput');
                    const errorDiv = document.getElementById('errorMsg');
                    errorDiv.textContent = '';
                    
                    if (fileInput.files.length === 0) {
                        errorDiv.textContent = 'Please select a file';
                        return false;
                    }
                    
                    const file = fileInput.files[0];
                    const maxSize = 50 * 1024 * 1024; // 50MB
                    
                    // Check file extension
                    if (!file.name.toLowerCase().endsWith('.zip')) {
                        errorDiv.textContent = 'Only .zip files are allowed';
                        return false;
                    }
                    
                    // Check file size
                    if (file.size > maxSize) {
                        errorDiv.textContent = 'File exceeds maximum size of 50MB';
                        return false;
                    }
                    
                    return true;
                }
            """
            ),
        )
    except Exception as e:
        print(f"Error loading admin dashboard: {str(e)}")
        return Titled(
            "Admin Dashboard",
            P(
                "Error loading recent uploads. Please try again later.",
                style="color: red;",
            ),
            A("Back to Home", href="/"),
        )


@rt("/admin/upload", methods=["POST"])
async def admin_upload_process(req):
    """Handle file uploads with validation and conflict resolution"""
    form = await req.form()
    file = form.get("file")
    semester = form.get("semester")
    batch_year = form.get("batch_year")
    password = form.get("password")

    if password != ADMIN_PASSWD:
        return Titled(
            "Error", P("Invalid password."), A("Back", href="javascript:history.back()")
        )

    # Validate required fields
    if not all([file, semester, batch_year]):
        return Titled(
            "Error",
            P("Missing required fields."),
            A("Back", href="javascript:history.back()"),
        )

    filename = f"{batch_year}.zip"  # Enforce naming convention

    # Client-side validation (should match server-side)
    if not file.filename.lower().endswith(".zip"):
        return Titled(
            "Error",
            P("Only .zip files are allowed."),
            A("Back", href="javascript:history.back()"),
        )

    if file.size > 50 * 1024 * 1024:  # 50MB
        return Titled(
            "Error",
            P("File exceeds maximum size of 50MB."),
            A("Back", href="javascript:history.back()"),
        )

    try:
        file_bytes = await file.read()

        # Verify ZIP integrity before processing
        try:
            with zipfile.ZipFile(io.BytesIO(file_bytes)) as zip_ref:
                if zip_ref.testzip() is not None:
                    raise ValueError("ZIP file contains corrupt files")
        except zipfile.BadZipfile:
            return Titled(
                "Error",
                P("Invalid ZIP file format."),
                A("Back", href="javascript:history.back()"),
            )

        # Check for existing file
        existing_files = list_files_in_semester(semester)
        existing_file = next(
            (f for f in existing_files if f["filename"] == filename), None
        )

        if not existing_file:
            # New upload
            try:
                file_id = upload_file_to_drive(file_bytes, filename, semester)
                return RedirectResponse("/admin", status_code=303)
            except Exception as e:
                return Titled(
                    "Error",
                    P(f"Upload failed: {str(e)}"),
                    A("Back", href="javascript:history.back()"),
                )
        else:
            # Conflict resolution needed
            temp_path = TEMP_UPLOADS / f"temp_{uuid.uuid4().hex}.zip"
            try:
                temp_path.write_bytes(file_bytes)
                return conflict_resolution_page(
                    temp_path=temp_path,
                    existing_file_id=existing_file["filepath"],
                    batch_year=batch_year,
                    semester=semester,
                )
            except Exception as e:
                if temp_path.exists():
                    temp_path.unlink()
                return Titled(
                    "Error",
                    P(f"Error preparing for conflict resolution: {str(e)}"),
                    A("Back", href="javascript:history.back()"),
                )

    except Exception as e:
        return Titled(
            "Error",
            P(f"Upload processing failed: {str(e)}"),
            A("Back", href="javascript:history.back()"),
        )


@rt("/admin/upload/resolve", methods=["POST"])
async def admin_upload_resolve(req):
    form = await req.form()
    temp_file, existing_file_id = Path(form["temp"]), form["existing"]
    action, confirm1, confirm2, password = (
        form["action"],
        form.get("confirm1", ""),
        form.get("confirm2", ""),
        form.get("password"),
    )
    semester = form.get("semester")
    batch_year = form.get("batch_year")  # Get batch_year from form

    if password != ADMIN_PASSWD:
        return Titled(
            "Error", P("Invalid password."), A("Back", href="javascript:history.back()")
        )

    if not semester or not batch_year:
        return Titled(
            "Error",
            P("Missing semester or batch year."),
            A("Back", href="javascript:history.back()"),
        )

    filename = f"{batch_year}.zip"  # Proper filename

    if action == "remove" and confirm1 == "REMOVE" and confirm2 == "REMOVE":
        try:
            # Delete existing file
            DRIVE_SERVICE.files().delete(fileId=existing_file_id).execute()

            # Upload new file with proper name
            with open(temp_file, "rb") as f:
                file_bytes = f.read()
            upload_file_to_drive(file_bytes, filename, semester)

            temp_file.unlink()
            return RedirectResponse("/admin", status_code=303)
        except Exception as e:
            if temp_file.exists():
                temp_file.unlink()
            return Titled(
                "Error",
                P(f"Error during replacement: {str(e)}"),
                A("Back", href="javascript:history.back()"),
            )

    elif action == "merge":
        try:
            with open(temp_file, "rb") as f:
                new_file_bytes = f.read()
            merge_zip_files_in_drive(existing_file_id, new_file_bytes)
            temp_file.unlink()
            return RedirectResponse("/admin", status_code=303)
        except Exception as e:
            if temp_file.exists():
                temp_file.unlink()
            return Titled(
                "Error",
                P(f"Merge failed: {str(e)}"),
                A("Back", href="javascript:history.back()"),
            )

    if temp_file.exists():
        temp_file.unlink()
    return Titled(
        "Error",
        P("Resolution not confirmed or invalid action."),
        A("Back", href="javascript:history.back()"),
    )


def conflict_resolution_page(temp_path, existing_file_id, batch_year, semester):
    return Titled(
        "Conflict Resolution",
        P(
            f"A zip for batch {batch_year} already exists in {semester}. Choose resolution:"
        ),
        Form(method="post", action="/admin/upload/resolve")(
            Input(name="temp", type="hidden", value=str(temp_path)),
            Input(name="existing", type="hidden", value=existing_file_id),
            Input(name="semester", type="hidden", value=semester),
            Input(name="batch_year", type="hidden", value=batch_year),  # Add batch_year
            Label(
                "Resolution",
                Select(
                    Option("Remove & Replace", value="remove"),
                    Option("Merge", value="merge"),
                    name="action",
                ),
            ),
            Label(
                "Type REMOVE for confirmation 1",
                Input(name="confirm1", type="text"),
            ),
            Label(
                "Type REMOVE for confirmation 2",
                Input(name="confirm2", type="text"),
            ),
            Label("Admin Password:", Input(type="password", name="password")),
            Button("Resolve", type="submit"),
        ),
        A("Back", href="javascript:history.back()"),
    )


@rt("/admin/delete", methods=["GET", "POST"])
async def admin_delete(req):
    file_id = req.query_params.get("file")

    if not file_id:
        print("Error: No file ID provided in the query parameters.")
        return PlainTextResponse("Invalid file parameter", status_code=400)

    if req.method == "GET":
        # Fetch file metadata to display filename
        try:
            print(f"Attempting to fetch file metadata for file ID: {file_id}")
            file_metadata = (
                DRIVE_SERVICE.files().get(fileId=file_id, fields="name").execute()
            )
            filename = file_metadata.get("name", "Unknown File")
            print(f"File name retrieved: {filename}")
        except errors.HttpError as error:  # Catch the Google API errors specifically
            print(f"An error occurred: {error}")
            filename = "Unknown File"
        except Exception as e:
            print(f"An unexpected error occurred: {e}")
            filename = "Unknown File"

        return Titled(
            "Confirm Deletion",
            P(f"Are you sure you want to delete {filename}?"),
            Form(method="post")(
                Input(type="hidden", name="file", value=file_id),
                Label(
                    "Type DELETE to confirm:", Input(type="text", name="confirmation")
                ),
                Label("Admin Password:", Input(type="password", name="password")),
                Button("Delete", type="submit"),
            ),
            A("Back", href="javascript:history.back()"),
        )

    form = await req.form()
    confirmation = form.get("confirmation")
    password = form.get("password")

    print(f"Confirmation input: {confirmation}")
    print(
        f"Password input: {'*' * len(password) if password else None}"  # Mask password
    )

    if confirmation == "DELETE" and password == ADMIN_PASSWD:
        try:
            # Check file modification time
            print(f"Attempting to fetch file modification time for file ID: {file_id}")
            file_metadata = (
                DRIVE_SERVICE.files()
                .get(fileId=file_id, fields="modifiedTime")
                .execute()
            )
            mod_time = datetime.datetime.fromisoformat(
                file_metadata.get("modifiedTime", "").replace("Z", "+00:00")
            )

            print(f"File modification time: {mod_time}")

            # Make utcnow() timezone-aware
            utc_now = datetime.datetime.utcnow().replace(tzinfo=pytz.utc)

            # Check if file is within 24 hours
            if (utc_now - mod_time).total_seconds() <= 86400:
                print(f"Attempting to delete file with ID: {file_id}")
                DRIVE_SERVICE.files().delete(fileId=file_id).execute()
                print(f"Successfully deleted file with ID: {file_id}")
            else:
                print("Error: Cannot delete file older than 24 hours")
                return Titled(
                    "Error",
                    P("Cannot delete file older than 24 hours."),
                    A("Back", href="javascript:history.back()"),
                )
        except errors.HttpError as error:  # Catch the Google API errors specifically
            print(f"An Google API error occurred: {error}")
            return Titled(
                "Error",
                P(f"Deletion failed: {str(error)}"),
                A("Back", href="javascript:history.back()"),
            )
        except Exception as e:
            print(f"An unexpected error occurred during deletion: {e}")
            return Titled(
                "Error",
                P(f"Deletion failed: {str(e)}"),
                A("Back", href="javascript:history.back()"),
            )
        return RedirectResponse("/admin", status_code=303)
    else:
        return Titled(
            "Error",
            P("Invalid confirmation or password."),
            A("Back", href="javascript:history.back()"),
        )


# === USER INTERFACE ===


@rt("/")
def user_index(req):
    return Titled(
        "Data Science Resources",
        Ul(*[Li(A(f"Semester {i}", href=f"/semester/{i}")) for i in range(1, 9)]),
        A("Admin", href="/admin"),
    )


@rt("/semester/{num}", methods=["GET", "POST"])
async def semester_view(req: Request, num: int):
    semester = f"Semester_{num}"
    if semester not in SEMESTER_FOLDER_IDS:
        return Response("Semester not found", status_code=404)

    files = list_files_in_semester(semester)

    if req.method == "POST":
        return RedirectResponse(f"/semester/{num}/download", status_code=303)

    form_content = [
        P(f"Total {len(files)} files"),
        Ul(
            *[
                Li(
                    Label(
                        Input(type="checkbox", name="selected", value=f["filepath"]),
                        " ",
                        Span(f["filename"]),
                    )
                )
                for f in files
            ]
        ),
        Div(
            Button(
                "Download Selected",
                type="submit",
                name="action",
                value="selected",
                formaction=f"/semester/{num}/download",
                id="selectedButton",
                style="display: none; margin-right: 10px;",
            ),
            Button(
                "Download All",
                type="submit",
                name="action",
                value="all",
                formaction=f"/semester/{num}/download",
                id="downloadAllButton",
                style="display: none;",
            ),
            Script(
                """
                document.addEventListener('DOMContentLoaded', function() {
                    const checkboxes = document.querySelectorAll('input[name="selected"]');
                    const selectedButton = document.getElementById('selectedButton');
                    const downloadAllButton = document.getElementById('downloadAllButton');
                    
                    function updateButtonVisibility() {
                        const anyChecked = Array.from(checkboxes).some(cb => cb.checked);
                        if (anyChecked) {
                            selectedButton.style.display = 'inline-block';
                            downloadAllButton.style.display = 'inline-block';
                        } else {
                            selectedButton.style.display = 'none';
                            downloadAllButton.style.display = 'none';
                        }
                    }
                    
                    checkboxes.forEach(checkbox => {
                        checkbox.addEventListener('change', updateButtonVisibility);
                    });
                    
                    updateButtonVisibility();
                });
                """
            ),
        ),
        A("Back to Home", href="/"),
    ]

    return Titled(f"Semester {num}", Form(*form_content, method="post"))


@rt("/semester", methods=["GET", "POST"])
async def semester_select(req):
    # Handle form submission
    if req.method == "POST":
        form = await req.form()
        semester_num = form.get("semester")
        if semester_num:
            return RedirectResponse(
                f"/semester/{semester_num}", status_code=303
            )  # Redirect to semester view
        else:
            return PlainTextResponse("No semester selected")

    # Build the HTML form
    form_content = [
        Label("Select Semester:"),
        Select(*[Option(str(i), value=str(i)) for i in range(1, 9)], name="semester"),
        Button("View Semester Files", type="submit"),
    ]
    return Titled(
        "Select Semester",
        Form(form_content, method="post"),
        A("Back to Home", href="/"),
    )


@rt("/semester/{num}/download", methods=["POST"])
async def semester_download(req, num: int):
    form = await req.form()
    action = form.get("action")
    semester = f"Semester_{num}"

    try:
        if action == "selected":
            selected = form.getlist("selected")
            if not selected:
                raise ValueError("No files selected")
            file_ids = selected

        elif action == "all":
            files = list_files_in_semester(semester)
            file_ids = [f["filepath"] for f in files]

        else:
            raise ValueError("Invalid action")

        # Create in-memory ZIP file
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w") as zf:
            for file_id in file_ids:
                try:
                    file_content = download_file_from_drive(file_id)
                    file_info = DRIVE_SERVICE.files().get(fileId=file_id).execute()
                    zf.writestr(file_info["name"], file_content)
                except Exception as e:
                    print(f"Error processing {file_id}: {str(e)}")
                    continue

        zip_buffer.seek(0)
        return Response(
            content=zip_buffer.getvalue(),
            media_type="application/zip",
            headers={
                "Content-Disposition": f"attachment; filename=semester_{num}_files.zip"
            },
        )

    except Exception as e:
        return Titled(
            "Download Error", P(str(e)), A("Back", href="javascript:history.back()")
        )


@rt("/download")
def download_file(req):
    try:
        file_id = req.query_params["file"]
        file_content = download_file_from_drive(file_id)
        temp_file = TEMP_UPLOADS / f"{uuid.uuid4().hex}.zip"
        temp_file.write_bytes(file_content)
        return FileResponse(str(temp_file), filename=temp_file.name)
    except Exception:
        return PlainTextResponse("Invalid file parameter", status_code=400)


# === APPLICATION INITIALIZATION ===
# Initialize Drive
try:
    SEMESTER_FOLDER_IDS = initialize_drive()
    print("Drive initialization successful")
except Exception as e:
    print(f"Fatal error during initialization: {str(e)}")
    print("Cannot continue without proper Drive access")
    exit(1)


# Ensure temp uploads directory exists
TEMP_UPLOADS = Path("/tmp/temp_uploads")
TEMP_UPLOADS.mkdir(parents=True, exist_ok=True)
# === RUN THE APP ===
setup_hf_backup(app)
serve(port=7860)
