from os import read
import ckan.plugins as plugins
import ckan.plugins.toolkit as toolkit
from six import text_type
from flask import Blueprint, request, jsonify
import ckan.lib.helpers as h
from PIL import Image
import io
import ckan.lib.uploader as uploader
import base64
import logging

log = logging.getLogger(__name__)
ignore_empty = plugins.toolkit.get_validator('ignore_empty')


def convert(): 
    try:
        resource_id = request.form.get('resource_id')    
        rsc = toolkit.get_action('resource_show')({}, {'id': resource_id})
        upload = uploader.get_resource_uploader(rsc)
        filepath = upload.get_path(rsc['id'])
        
        log.info(f"Processing image: {filepath}")
        
        # Check if file exists and get size
        import os
        if not os.path.exists(filepath):
            raise FileNotFoundError(f"File not found: {filepath}")
        
        file_size = os.path.getsize(filepath)
        log.info(f"File size: {file_size} bytes")
        
        # Try to detect file type
        import imghdr
        file_type = imghdr.what(filepath)
        log.info(f"Detected file type: {file_type}")
        
        # For GeoTIFF files, try using rasterio first if available
        if file_type is None or file_type == 'tiff':
            try:
                import rasterio
                from rasterio.plot import reshape_as_image
                import numpy as np
                
                log.info("Attempting to open with rasterio (GeoTIFF handler)")
                with rasterio.open(filepath) as src:
                    # Read the first 3 bands or just 1 if grayscale
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
                log.info(f"Rasterio not available or failed ({e}), falling back to PIL")
                # Fall back to PIL
                with open(filepath, "rb") as f:
                    img = Image.open(f)
                    img.load()
        else:
            # Use PIL for standard image formats
            with open(filepath, "rb") as f:
                img = Image.open(f)
                img.load()
        
        # For multi-band images (like GeoTIFF), convert to RGB
        if img.mode not in ('RGB', 'L'):
            if img.mode == 'RGBA':
                # Handle transparency
                background = Image.new('RGB', img.size, (255, 255, 255))
                background.paste(img, mask=img.split()[3] if len(img.split()) == 4 else None)
                img = background
            elif img.mode in ('I', 'I;16', 'F'):
                # Handle 16-bit or float images - normalize to 8-bit
                import numpy as np
                img_array = np.array(img)
                # Normalize to 0-255 range
                img_min = img_array.min()
                img_max = img_array.max()
                if img_max > img_min:
                    img_array = ((img_array - img_min) / (img_max - img_min) * 255).astype(np.uint8)
                else:
                    img_array = np.zeros_like(img_array, dtype=np.uint8)
                img = Image.fromarray(img_array)
            else:
                img = img.convert('RGB')
        
        # Create thumbnail for large images to avoid memory issues
        max_size = (2048, 2048)
        if img.size[0] > max_size[0] or img.size[1] > max_size[1]:
            log.info(f"Resizing image from {img.size} to max {max_size}")
            img.thumbnail(max_size, Image.Resampling.LANCZOS)
        
        # Convert to JPEG
        output = io.BytesIO()
        if img.mode == 'L':
            img.convert('RGB').save(output, 'JPEG', quality=85)
        else:
            img.save(output, 'JPEG', quality=85)
        output.seek(0)
        
        log.info("Image conversion successful")
        return base64.b64encode(output.getvalue()).decode()
    
    except Exception as e:
        log.error(f"Error converting image: {str(e)}", exc_info=True)
        return jsonify({'error': f'Cannot process image: {str(e)}'}), 500


class TifImageviewPlugin(plugins.SingletonPlugin):
    plugins.implements(plugins.IConfigurer)
    plugins.implements(plugins.IResourceView, inherit=True)
    plugins.implements(plugins.IBlueprint)



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
            u'/tif_view/convert',
            u'convert',
            convert,
            methods=['POST']
            )
        
        return blueprint