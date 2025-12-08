from os import read
import ckan.plugins as plugins
import ckan.plugins.toolkit as toolkit
from six import text_type
from flask import Blueprint, request, jsonify, send_file
import ckan.lib.helpers as h
from PIL import Image
import io
import ckan.lib.uploader as uploader
import base64
import logging
import os

log = logging.getLogger(__name__)
ignore_empty = plugins.toolkit.get_validator('ignore_empty')


def get_preview_image():
    """Serve the cached JPEG preview or generate it if missing"""
    try:
        resource_id = request.args.get('resource_id')
        if not resource_id:
            return jsonify({'error': 'Missing resource_id'}), 400
            
        rsc = toolkit.get_action('resource_show')({}, {'id': resource_id})
        upload = uploader.get_resource_uploader(rsc)
        
        # Check if this is S3 uploader by class name
        uploader_class = upload.__class__.__name__
        is_s3 = 'S3' in uploader_class or hasattr(upload, 'get_url_from_filename')
        
        if is_s3:
            # For S3, try to get cached preview or generate it
            preview_url = _get_or_create_s3_preview(rsc, upload)
            if preview_url:
                # Redirect to S3 URL (let S3 serve it)
                from flask import redirect
                return redirect(preview_url)
            else:
                return jsonify({'error': 'Failed to generate preview'}), 500
        else:
            # Local file store - use cached JPEG
            filepath = upload.get_path(rsc['id'])
            preview_path = _get_preview_path(filepath)
            
            # Generate preview if it doesn't exist
            if not os.path.exists(preview_path):
                log.info(f"Preview not found, generating: {preview_path}")
                _generate_and_save_preview(filepath, preview_path)
            
            # Serve the cached JPEG
            return send_file(preview_path, mimetype='image/jpeg', as_attachment=False)
    
    except Exception as e:
        log.error(f"Error serving preview: {str(e)}", exc_info=True)
        return jsonify({'error': f'Cannot serve preview: {str(e)}'}), 500


def _get_or_create_s3_preview(resource, upload):
    """Get S3 preview URL (preview should already exist from after_resource_create hook)"""
    try:
        # Construct preview filename
        base_name = os.path.splitext(resource.get('name', 'file.tif'))[0]
        preview_filename = f"{base_name}_preview.jpg"
        
        # Build preview URL
        resource_url = resource.get('url', '')
        if resource_url:
            if resource_url.endswith('.tif') or resource_url.endswith('.tiff'):
                base_url = resource_url.rsplit('.', 1)[0]
                preview_url = f"{base_url}_preview.jpg"
            else:
                preview_url = f"{resource_url}_preview.jpg"
        else:
            from ckan.common import config
            site_url = config.get('ckan.site_url', 'http://localhost:5000')
            package_id = resource.get('package_id')
            resource_id = resource.get('id')
            preview_url = f"{site_url}/dataset/{package_id}/resource/{resource_id}/download/{preview_filename}"
        
        log.debug(f"Preview URL: {preview_url}")
        return preview_url
        
    except Exception as e:
        log.error(f"Error getting preview URL: {e}", exc_info=True)
        return None


def _generate_and_upload_preview(resource, preview_filename):
    """Download TIF from storage, convert to JPEG, and upload using CKAN uploader"""
    import requests
    import tempfile
    from werkzeug.datastructures import FileStorage
    import time
    
    # Step 1: Wait for original file to be fully uploaded and get its download URL
    # Refresh resource metadata to ensure we have the latest info
    max_retries = 5
    download_url = None
    
    for attempt in range(max_retries):
        try:
            # Refresh resource to get updated metadata
            refreshed_resource = toolkit.get_action('resource_show')({}, {'id': resource['id']})
            url = refreshed_resource.get('url', '')
            
            # Check if we have a proper download URL
            if url and url.startswith(('http://', 'https://')):
                download_url = url
                log.info(f"Got download URL: {download_url}")
                break
            elif url:
                # URL exists but is relative or just filename - file should be uploaded
                log.info(f"File appears uploaded, URL: {url}")
                break
            else:
                log.info(f"Waiting for file upload to complete (attempt {attempt + 1}/{max_retries})")
                time.sleep(1)
        except Exception as e:
            log.warning(f"Error checking resource status: {e}")
            time.sleep(1)
    
    # Step 2: Get the actual file
    upload = uploader.get_resource_uploader(resource)
    uploader_class = upload.__class__.__name__
    is_s3 = 'S3' in uploader_class
    
    log.info(f"Accessing file with {uploader_class}")
    
    suffix = os.path.splitext(resource.get('name', ''))[1] or '.tif'
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temp_tif:
        temp_tif_path = temp_tif.name
        
        if is_s3:
            # For S3: Use the S3 client from ckanext-s3filestore uploader
            # This ensures we use the exact same configuration and credentials
            from ckan.common import config
            
            # Get S3 configuration
            storage_path = config.get('ckanext.s3filestore.aws_storage_path', '')
            
            # Construct S3 key - extract filename from URL since resource['name'] may be outdated
            resource_id = resource.get('id')
            
            # Try to get actual filename from uploader first (most reliable)
            try:
                actual_filename = upload.get_path(resource_id).split('/')[-1]
                log.info(f"Got filename from uploader path: {actual_filename}")
            except:
                # Fallback: extract from URL
                url = refreshed_resource.get('url', '')
                if url and '/download/' in url:
                    # Extract filename from URL like: .../download/2025-12-08-11-26-11/small_world.tif
                    actual_filename = url.split('/download/')[-1].split('/')[-1]
                    log.info(f"Extracted filename from URL: {actual_filename}")
                else:
                    # Last resort: use resource name
                    actual_filename = resource.get('name', 'file.tif')
                    log.info(f"Using resource name: {actual_filename}")
            
            if storage_path:
                s3_key = f"{storage_path}/resources/{resource_id}/{actual_filename}"
            else:
                s3_key = f"resources/{resource_id}/{actual_filename}"
            
            # Get the S3 client from the uploader instance (reuse its configuration)
            s3_client = upload.get_s3_client()
            bucket_name = upload.bucket_name
            
            log.info(f"Generating presigned URL for S3: bucket={bucket_name}, key={s3_key}")
            log.info(f"Using uploader config: region={upload.region}, signature={upload.signature}, addressing={upload.addressing_style}")
            
            # Generate presigned URL (valid for 5 minutes)
            presigned_url = s3_client.generate_presigned_url(
                'get_object',
                Params={'Bucket': bucket_name, 'Key': s3_key},
                ExpiresIn=300
            )
            
            log.info(f"Downloading from presigned URL (this may take several minutes for large files)")
            
            # Download using requests with presigned URL (direct S3 access, bypasses CKAN)
            import requests
            
            try:
                # Stream download to handle large files efficiently
                response = requests.get(presigned_url, stream=True, timeout=(30, 600))
                response.raise_for_status()
                
                # Write chunks to temp file
                bytes_downloaded = 0
                chunk_size = 8192  # 8KB chunks
                
                with open(temp_tif.name, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=chunk_size):
                        if chunk:
                            f.write(chunk)
                            bytes_downloaded += len(chunk)
                            
                            # Log progress every 10MB
                            if bytes_downloaded % (10 * 1024 * 1024) < chunk_size:
                                log.debug(f"Downloaded {bytes_downloaded / (1024 * 1024):.1f} MB...")
                
                log.info(f"Downloaded {bytes_downloaded} bytes using presigned URL")
                
                # Check file size
                if bytes_downloaded < 1000:
                    # File too small - likely an error message
                    with open(temp_tif.name, 'r') as f:
                        content = f.read()
                    log.error(f"Downloaded file too small ({bytes_downloaded} bytes), S3 error: {content[:500]}")
                    
                    # Parse error details
                    error_msg = "Failed to download file from S3"
                    if 'InvalidAccessKeyId' in content:
                        error_msg = "S3 credentials are invalid. The AWS Access Key ID does not exist. Please check CKANEXT__S3FILESTORE__AWS_ACCESS_KEY_ID in .env"
                    elif 'AccessDenied' in content or 'Forbidden' in content:
                        error_msg = "S3 access denied. Ensure the AWS credentials have 's3:GetObject' permission for the bucket"
                    elif 'NoSuchKey' in content:
                        error_msg = f"File not found in S3. Check the file was uploaded successfully. Key: {s3_key}"
                    else:
                        error_msg = f"S3 error: {content[:200]}"
                    
                    raise Exception(error_msg)
                
            except requests.exceptions.Timeout:
                raise Exception("Download timed out after 10 minutes. File may be too large.")
            except requests.exceptions.RequestException as e:
                log.error(f"Download failed: {e}")
                raise Exception(f"Failed to download file: {e}")
        else:
            # For local storage: Read directly from file path
            log.info(f"Reading from local storage")
            filepath = upload.get_path(resource['id'])
            with open(filepath, 'rb') as source_file:
                temp_tif.write(source_file.read())
        
        temp_tif.flush()
    
    try:
        # Step 2: Convert to JPEG in another temp file
        with tempfile.NamedTemporaryFile(delete=False, suffix='.jpg') as temp_jpg:
            temp_jpg_path = temp_jpg.name
        
        try:
            with open(temp_jpg_path, 'wb') as output_file:
                _process_and_save_image(temp_tif_path, output_file)
            
            log.info(f"Converted to JPEG: {temp_jpg_path}")
            
            # Step 3: Upload using CKAN uploader
            with open(temp_jpg_path, 'rb') as jpg_file:
                # Create a FileStorage object that CKAN uploader expects
                file_storage = FileStorage(
                    stream=jpg_file,
                    filename=preview_filename,
                    content_type='image/jpeg'
                )
                
                # Create a resource dict for the preview
                preview_resource = {
                    'id': resource['id'],
                    'url': preview_filename,
                    'url_type': 'upload',
                    'upload': file_storage  # Pass the file to the uploader
                }
                
                # Get uploader and upload the file
                # The uploader will use credentials from CKAN config (works for both local MinIO and cloud S3)
                preview_upload = uploader.get_resource_uploader(preview_resource)
                preview_upload.upload(resource['id'], max_size=10)
                
                log.info(f"Uploaded preview using CKAN uploader: {preview_filename}")
        finally:
            # Clean up JPEG temp file
            if os.path.exists(temp_jpg_path):
                os.unlink(temp_jpg_path)
    finally:
        # Clean up TIF temp file
        if os.path.exists(temp_tif_path):
            os.unlink(temp_tif_path)


def _get_preview_path(original_path):
    """Get the path for the cached JPEG preview"""
    base, _ = os.path.splitext(original_path)
    return f"{base}_preview.jpg"


def _generate_and_save_preview(source_path, preview_path):
    """Generate JPEG preview from TIF and save to disk"""
    try:
        with open(preview_path, 'wb') as output_file:
            _process_and_save_image(source_path, output_file)
        log.info(f"Saved preview: {preview_path}")
    except Exception as e:
        log.error(f"Failed to generate preview: {e}", exc_info=True)
        raise


def _process_and_save_image(source_path, output_stream):
    """Process TIF image and save as JPEG to output_stream"""
    import imghdr
    
    file_type = imghdr.what(source_path)
    log.info(f"Processing {source_path}, detected type: {file_type}")
    
    # Try rasterio for GeoTIFF first
    if file_type is None or file_type == 'tiff':
        try:
            import rasterio
            from rasterio.plot import reshape_as_image
            import numpy as np
            
            with rasterio.open(source_path) as src:
                # Read bands
                if src.count >= 3:
                    data = src.read([1, 2, 3])
                    data = reshape_as_image(data)
                else:
                    data = src.read(1)
                
                # Normalize to 8-bit
                if data.dtype != np.uint8:
                    data_min = np.nanmin(data)
                    data_max = np.nanmax(data)
                    if data_max > data_min:
                        data = ((data - data_min) / (data_max - data_min) * 255).astype(np.uint8)
                    else:
                        data = np.zeros_like(data, dtype=np.uint8)
                
                img = Image.fromarray(data)
                
        except (ImportError, Exception) as e:
            log.info(f"Rasterio failed ({e}), using PIL")
            with open(source_path, "rb") as f:
                img = Image.open(f)
                img.load()
    else:
        with open(source_path, "rb") as f:
            img = Image.open(f)
            img.load()
    
    # Convert to RGB
    if img.mode not in ('RGB', 'L'):
        if img.mode == 'RGBA':
            background = Image.new('RGB', img.size, (255, 255, 255))
            background.paste(img, mask=img.split()[3] if len(img.split()) == 4 else None)
            img = background
        elif img.mode in ('I', 'I;16', 'F'):
            import numpy as np
            img_array = np.array(img)
            img_min = img_array.min()
            img_max = img_array.max()
            if img_max > img_min:
                img_array = ((img_array - img_min) / (img_max - img_min) * 255).astype(np.uint8)
            else:
                img_array = np.zeros_like(img_array, dtype=np.uint8)
            img = Image.fromarray(img_array)
        else:
            img = img.convert('RGB')
    
    # Resize large images
    max_size = (2048, 2048)
    if img.size[0] > max_size[0] or img.size[1] > max_size[1]:
        log.info(f"Resizing from {img.size} to max {max_size}")
        img.thumbnail(max_size, Image.Resampling.LANCZOS)
    
    # Save as JPEG
    if img.mode == 'L':
        img.convert('RGB').save(output_stream, 'JPEG', quality=85)
    else:
        img.save(output_stream, 'JPEG', quality=85)


class TifImageviewPlugin(plugins.SingletonPlugin):
    plugins.implements(plugins.IConfigurer)
    plugins.implements(plugins.IResourceView, inherit=True)
    plugins.implements(plugins.IBlueprint)
    plugins.implements(plugins.IResourceController, inherit=True)



    # IConfigurer

    def update_config(self, config_):
        toolkit.add_template_directory(config_, 'theme/templates')
        toolkit.add_public_directory(config_, 'public')
        toolkit.add_resource('fanstatic', 'tif_imageview')
        self.formats = config_.get(
            'ckan.preview.image_formats',
            'tiff tif TIFF').split()
        
        
    def info(self):
        return {'name': 'tif_imageview',
            'title': plugins.toolkit._('TIF Viewer'),
            'schema': {'tif_url': [ignore_empty, text_type]},
            'iframed': False,
            'icon': 'link',
            'always_available': True,
            'default_title': plugins.toolkit._('TIF Viewer'),
        }
    
    def can_view(self, data_dict):
        resource = data_dict['resource']
        return (resource.get('format', '').lower() in ['tif', 'tiff' ] or
                resource['url'].split('.')[-1] in ['tif'])

    def view_template(self, context, data_dict):
        return 'tif_view.html'

    def form_template(self, context, data_dict):
        return 'tif_form.html'

    def get_blueprint(self):

        blueprint = Blueprint(self.name, self.__module__)
        blueprint.template_folder = u'templates'
        blueprint.add_url_rule(
            u'/tif_view/preview',
            u'preview',
            get_preview_image,
            methods=['GET']
            )
        
        return blueprint
    
    # IResourceController
    
    def after_resource_create(self, context, resource):
        """Generate preview asynchronously after resource creation"""
        self._generate_preview_if_tif(resource)
    
    def after_resource_update(self, context, resource):
        """Regenerate preview asynchronously after resource update"""
        self._generate_preview_if_tif(resource, force_regenerate=True)
    
    def _generate_preview_if_tif(self, resource, force_regenerate=False):
        """Generate preview JPEG if resource is a TIF file (runs in background thread)
        
        Args:
            resource: The resource dict
            force_regenerate: If True, delete existing preview before generating new one
        """
        try:
            # Check if it's a TIF file
            format_lower = resource.get('format', '').lower()
            url = resource.get('url', '')
            
            if format_lower not in ['tif', 'tiff'] and not url.endswith(('.tif', '.tiff')):
                return
            
            # Run in background thread to avoid blocking the request
            import threading
            
            def generate_preview_task():
                try:
                    log.info(f"Starting background preview generation for resource: {resource['id']}")
                    
                    # Construct preview filename
                    base_name = os.path.splitext(resource.get('name', 'file.tif'))[0]
                    preview_filename = f"{base_name}_preview.jpg"
                    
                    # If force_regenerate, delete existing preview first
                    if force_regenerate:
                        try:
                            upload = uploader.get_resource_uploader(resource)
                            uploader_class = upload.__class__.__name__
                            is_s3 = 'S3' in uploader_class
                            
                            if is_s3:
                                # Delete from S3
                                from ckan.common import config
                                storage_path = config.get('ckanext.s3filestore.aws_storage_path', '')
                                resource_id = resource.get('id')
                                
                                if storage_path:
                                    preview_key = f"{storage_path}/resources/{resource_id}/{preview_filename}"
                                else:
                                    preview_key = f"resources/{resource_id}/{preview_filename}"
                                
                                s3_client = upload.get_s3_client()
                                bucket_name = upload.bucket_name
                                
                                log.info(f"Deleting existing preview from S3: {preview_key}")
                                s3_client.delete_object(Bucket=bucket_name, Key=preview_key)
                                log.info(f"Deleted existing preview from S3")
                            else:
                                # Delete from local storage
                                filepath = upload.get_path(resource['id'])
                                preview_path = _get_preview_path(filepath)
                                if os.path.exists(preview_path):
                                    log.info(f"Deleting existing preview: {preview_path}")
                                    os.unlink(preview_path)
                                    log.info(f"Deleted existing preview")
                        except Exception as e:
                            log.warning(f"Could not delete existing preview (will overwrite): {e}")
                    
                    # Download, convert, and upload
                    _generate_and_upload_preview(resource, preview_filename)
                    
                    log.info(f"Successfully generated and uploaded preview in background: {preview_filename}")
                    
                except Exception as e:
                    log.error(f"Error generating preview in background: {e}", exc_info=True)
            
            # Start background thread
            thread = threading.Thread(target=generate_preview_task, daemon=True)
            thread.start()
            log.info(f"Preview generation started in background thread for resource: {resource['id']}")
                
        except Exception as e:
            log.error(f"Error starting preview generation thread: {e}", exc_info=True)