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
    """Get S3 preview URL or generate and upload if missing"""
    try:
        # Construct preview filename
        base_name = os.path.splitext(resource.get('name', 'file.tif'))[0]
        preview_filename = f"{base_name}_preview.jpg"
        
        # Build preview URL to check if it exists
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
        
        # Check if preview already exists in S3 using authenticated boto3
        from ckan.common import config
        import boto3
        from botocore.client import Config as BotoConfig
        from botocore.exceptions import ClientError
        
        bucket_name = config.get('ckanext.s3filestore.aws_bucket_name', 'ckan')
        resource_id = resource.get('id')
        region = config.get('ckanext.s3filestore.region_name', 'us-east-1')
        aws_access_key_id = config.get('ckanext.s3filestore.aws_access_key_id')
        aws_secret_access_key = config.get('ckanext.s3filestore.aws_secret_access_key')
        s3_host = config.get('ckanext.s3filestore.host_name')
        
        # S3 key for preview
        s3_preview_key = f"resources/{resource_id}/{preview_filename}"
        
        try:
            # Configure boto3 client
            s3_config = BotoConfig(signature_version='s3v4')
            endpoint_url = s3_host if s3_host and ('minio' in s3_host or ':' in s3_host.split('//')[-1]) else None
            
            s3_client = boto3.client(
                's3',
                aws_access_key_id=aws_access_key_id,
                aws_secret_access_key=aws_secret_access_key,
                region_name=region,
                endpoint_url=endpoint_url,
                config=s3_config
            )
            
            # Check if preview exists with head_object
            s3_client.head_object(Bucket=bucket_name, Key=s3_preview_key)
            log.info(f"Preview already exists in S3: {s3_preview_key}")
            return preview_url
        except ClientError as e:
            if e.response['Error']['Code'] == '404':
                log.debug(f"Preview not found in S3 (will generate): {s3_preview_key}")
            else:
                log.warning(f"Error checking preview in S3: {e}")
        except Exception as e:
            log.warning(f"Error checking preview existence: {e}")
        
        log.info(f"Generating preview for resource: {resource['id']}")
        
        # Download, convert, and upload preview
        _generate_and_upload_preview(resource, preview_filename)
        
        # Build preview URL based on the resource's URL pattern
        # Replace the original filename extension with _preview.jpg
        resource_url = resource.get('url', '')
        if resource_url:
            # For S3: URL like http://localhost:5000/dataset/.../download/file.tif
            # We need: http://localhost:5000/dataset/.../download/file_preview.jpg
            if resource_url.endswith('.tif') or resource_url.endswith('.tiff'):
                # Remove extension and add _preview.jpg
                base_url = resource_url.rsplit('.', 1)[0]
                preview_url = f"{base_url}_preview.jpg"
            else:
                # Fallback: just append _preview.jpg
                preview_url = f"{resource_url}_preview.jpg"
        else:
            # Fallback: construct from CKAN site URL
            from ckan.common import config
            site_url = config.get('ckan.site_url', 'http://localhost:5000')
            package_id = resource.get('package_id')
            resource_id = resource.get('id')
            preview_url = f"{site_url}/dataset/{package_id}/resource/{resource_id}/download/{preview_filename}"
        
        log.info(f"Preview URL: {preview_url}")
        return preview_url
        
    except Exception as e:
        log.error(f"Error with preview: {e}", exc_info=True)
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
    
    # Step 2: Get the actual file - use uploader to access it
    upload = uploader.get_resource_uploader(resource)
    uploader_class = upload.__class__.__name__
    is_s3 = 'S3' in uploader_class
    
    log.info(f"Accessing file with {uploader_class}")
    
    suffix = os.path.splitext(resource.get('name', ''))[1] or '.tif'
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temp_tif:
        temp_tif_path = temp_tif.name
        
        if is_s3:
            # For S3: Download using authenticated boto3 (works for both MinIO and AWS S3)
            # Cannot use CKAN URL from inside container as it causes routing issues
            from ckan.common import config
            import boto3
            from botocore.client import Config as BotoConfig
            
            # Get S3 configuration
            bucket_name = config.get('ckanext.s3filestore.aws_bucket_name', 'ckan')
            resource_id = resource.get('id')
            region = config.get('ckanext.s3filestore.region_name', 'us-east-1')
            aws_access_key_id = config.get('ckanext.s3filestore.aws_access_key_id')
            aws_secret_access_key = config.get('ckanext.s3filestore.aws_secret_access_key')
            s3_host = config.get('ckanext.s3filestore.host_name')
            
            # Get just the filename
            filename = resource.get('name', '')
            resource_url = resource.get('url', '')
            if resource_url and not resource_url.startswith(('http://', 'https://')):
                filename = resource_url
            
            # S3 key
            s3_key = f"resources/{resource_id}/{filename}"
            
            # Configure boto3 client
            s3_config = BotoConfig(signature_version='s3v4')
            # Use endpoint_url for MinIO (has hostname with port or 'minio'), None for AWS S3
            endpoint_url = s3_host if s3_host and ('minio' in s3_host or ':' in s3_host.split('//')[-1]) else None
            
            s3_client = boto3.client(
                's3',
                aws_access_key_id=aws_access_key_id,
                aws_secret_access_key=aws_secret_access_key,
                region_name=region,
                endpoint_url=endpoint_url,
                config=s3_config
            )
            
            log.info(f"Downloading from S3: bucket={bucket_name}, key={s3_key}")
            s3_client.download_fileobj(bucket_name, s3_key, temp_tif)
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
        """Generate preview after resource is created"""
        self._generate_preview_if_tif(resource)
    
    def after_resource_update(self, context, resource):
        """Regenerate preview after resource is updated"""
        self._generate_preview_if_tif(resource)
    
    def _generate_preview_if_tif(self, resource):
        """Generate preview JPEG if resource is a TIF file"""
        try:
            # Check if it's a TIF file
            format_lower = resource.get('format', '').lower()
            url = resource.get('url', '')
            
            if format_lower not in ['tif', 'tiff'] and not url.endswith(('.tif', '.tiff')):
                return
            
            upload = uploader.get_resource_uploader(resource)
            
            # Check if S3 uploader by checking class name
            uploader_class = upload.__class__.__name__
            is_s3 = 'S3' in uploader_class or hasattr(upload, 'get_url_from_filename')
            
            log.info(f"Preview generation for {resource['id']}, uploader: {uploader_class}, is_s3: {is_s3}")
            
            # Unified approach for both S3 and local storage
            # 1. Download using resource URL to temp file
            # 2. Convert to JPEG
            # 3. Upload using CKAN uploader (handles both S3 and local)
            try:
                log.info(f"Generating preview for resource: {resource['id']}, uploader: {uploader_class}")
                
                # Construct preview filename
                base_name = os.path.splitext(resource.get('name', 'file.tif'))[0]
                preview_filename = f"{base_name}_preview.jpg"
                
                # Download, convert, and upload
                _generate_and_upload_preview(resource, preview_filename)
                
                log.info(f"Successfully generated and uploaded preview: {preview_filename}")
                
            except Exception as e:
                log.error(f"Error generating preview: {e}", exc_info=True)
                
        except Exception as e:
            log.error(f"Error in preview generation hook: {e}", exc_info=True)