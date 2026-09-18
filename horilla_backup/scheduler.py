import os


from horilla import settings

from .gdrive import upload_file

from .models import GoogleDriveBackup
from .pgdump import dump_postgres_db
from .zip import zip_folder


def google_drive_backup():
    google_drive = GoogleDriveBackup.objects.filter(active=True).first()
    if google_drive:
        service_account_file = google_drive.service_account_file.path
        gdrive_folder_id = google_drive.gdrive_folder_id
        if google_drive.backup_db:
            db = settings.DATABASES["default"]
            dump_postgres_db(
                db_name=db["NAME"],
                username=db["USER"],
                output_file="backupdb.dump",
                password=db["PASSWORD"],
            )
            upload_file("backupdb.dump", service_account_file, gdrive_folder_id)
            os.remove("backupdb.dump")
        if google_drive.backup_media:
            folder_to_zip = settings.MEDIA_ROOT
            output_zip_file = "media.zip"
            zip_folder(folder_to_zip, output_zip_file)
            upload_file("media.zip", service_account_file, gdrive_folder_id)
            os.remove("media.zip")


def start_gdrive_backup_job():
    """Compatibility hook: the dedicated scheduler polls persisted configuration."""
    return None


def stop_gdrive_backup_job():
    """Compatibility hook: the dedicated scheduler polls persisted configuration."""
    return None
