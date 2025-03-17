import os
import datetime
import time
import threading
import hashlib
import logging
from flask import Flask, render_template, send_from_directory, request, jsonify
from PIL import Image
import io
import base64
import shutil

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('app.log'),
        logging.StreamHandler()
    ]
)

app = Flask(__name__)

# 配置文件夹路径
PHOTOS_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'photos')
CACHE_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cache')
THUMBNAIL_FOLDER = os.path.join(CACHE_FOLDER, 'thumbnails')
THUMBNAIL_MAX_SIZE = 800  # 缩略图最大尺寸（宽或高）

# 确保必要的文件夹存在
if not os.path.exists(PHOTOS_FOLDER):
    os.makedirs(PHOTOS_FOLDER)
if not os.path.exists(CACHE_FOLDER):
    os.makedirs(CACHE_FOLDER)
if not os.path.exists(THUMBNAIL_FOLDER):
    os.makedirs(THUMBNAIL_FOLDER)

# 图片缓存锁，防止并发操作导致的问题
cache_lock = threading.Lock()

# 允许的图片扩展名
ALLOWED_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif'}

# 退出标志，用于优雅退出缓存监控线程
exit_flag = False

# 全局标志，控制是否处于快速启动模式
FAST_START = True  # 默认使用快速启动模式

# 状态信息
cache_status = {
    "processing": False,
    "total": 0,
    "processed": 0,
    "percent": 0,
    "last_update": None
}

# 生成图片缓存的文件名
def get_cache_filename(original_filename, width, height):
    # 使用原始文件名和目标尺寸创建一个唯一的缓存文件名
    name, ext = os.path.splitext(original_filename)
    return f"{name}_{width}x{height}{ext}"

# 检查缓存是否存在且最新
def is_cache_valid(original_path, cache_path):
    # 如果缓存不存在，返回False
    if not os.path.exists(cache_path):
        return False
    
    # 检查原始文件是否比缓存更新
    original_mtime = os.path.getmtime(original_path)
    cache_mtime = os.path.getmtime(cache_path)
    
    # 如果原始文件更新，则缓存无效
    return original_mtime <= cache_mtime

# 生成并保存缩略图到缓存
def generate_thumbnail(file_path, cache_path, max_size=THUMBNAIL_MAX_SIZE):
    try:
        with Image.open(file_path) as img:
            # 获取原始图片尺寸
            orig_width, orig_height = img.size
            
            # 计算缩放比例，保持原始宽高比
            ratio = min(max_size / orig_width, max_size / orig_height)
            new_width = int(orig_width * ratio)
            new_height = int(orig_height * ratio)
            
            # 调整图片大小
            thumbnail = img.resize((new_width, new_height), Image.LANCZOS)
            
            # 保存到缓存文件夹
            thumbnail.save(cache_path, format=img.format)
            
            logging.info(f"已为 {os.path.basename(file_path)} 生成缩略图缓存")
            return thumbnail, new_width, new_height
    except Exception as e:
        logging.error(f"生成缩略图时出错: {e}")
        return None, 0, 0

# 清理不再需要的缓存
def clean_unused_cache():
    with cache_lock:
        try:
            # 获取所有原始图片的文件名
            original_filenames = set()
            for filename in os.listdir(PHOTOS_FOLDER):
                if os.path.isfile(os.path.join(PHOTOS_FOLDER, filename)) and \
                   os.path.splitext(filename)[1].lower() in ALLOWED_EXTENSIONS:
                    original_filenames.add(os.path.splitext(filename)[0])
            
            # 检查缓存文件夹中的每个文件
            deleted_count = 0
            for cache_dir in [THUMBNAIL_FOLDER]:
                if os.path.exists(cache_dir):
                    for cache_file in os.listdir(cache_dir):
                        is_valid = False
                        
                        # 遍历所有原始文件名，检查缓存文件是否匹配（前缀匹配）
                        for orig_name in original_filenames:
                            # 例如，原文件"photo123.jpg"的缓存可能是"photo123_800x800.jpg"
                            if cache_file.startswith(orig_name + "_"):
                                is_valid = True
                                break
                        
                        # 如果没找到匹配的原始文件，则删除缓存
                        if not is_valid:
                            cache_path = os.path.join(cache_dir, cache_file)
                            try:
                                os.remove(cache_path)
                                deleted_count += 1
                                logging.info(f"删除无用缓存: {cache_file}")
                            except Exception as e:
                                logging.error(f"删除缓存文件时出错: {e}")
            
            if deleted_count > 0:
                logging.info(f"共删除 {deleted_count} 个无用缓存文件")
        except Exception as e:
            logging.error(f"清理缓存时出错: {e}")

# 预处理所有图片并生成缓存
def preprocess_all_photos():
    global cache_status
    
    # 避免重复处理
    if cache_status["processing"]:
        logging.info("已有预处理任务正在进行中，跳过此次请求")
        return
    
    with cache_lock:
        try:
            cache_status["processing"] = True
            cache_status["processed"] = 0
            cache_status["percent"] = 0
            cache_status["last_update"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            # 获取所有需要处理的图片
            photos_to_process = []
            for filename in os.listdir(PHOTOS_FOLDER):
                file_path = os.path.join(PHOTOS_FOLDER, filename)
                ext = os.path.splitext(filename)[1].lower()
                
                if os.path.isfile(file_path) and ext in ALLOWED_EXTENSIONS:
                    cache_filename = get_cache_filename(filename, THUMBNAIL_MAX_SIZE, THUMBNAIL_MAX_SIZE)
                    cache_path = os.path.join(THUMBNAIL_FOLDER, cache_filename)
                    
                    # 检查缓存是否需要更新
                    if not is_cache_valid(file_path, cache_path):
                        photos_to_process.append((filename, file_path, cache_path))
            
            total_count = len(photos_to_process)
            cache_status["total"] = total_count
            
            if total_count == 0:
                logging.info("没有图片需要处理，缓存已是最新")
                # 清理不再需要的缓存
                clean_unused_cache()
                cache_status["processing"] = False
                return
            
            logging.info(f"发现 {total_count} 张图片需要处理缓存")
            
            # 处理每张图片（不在锁内）
            processed_count = 0
            for index, (filename, file_path, cache_path) in enumerate(photos_to_process):
                try:
                    # 报告进度
                    if index % 5 == 0 or index == total_count - 1:
                        progress = (index + 1) / total_count * 100
                        cache_status["processed"] = index + 1
                        cache_status["percent"] = round(progress, 1)
                        logging.info(f"处理缓存进度: {progress:.1f}% ({index+1}/{total_count})")
                    
                    # 为单张图片生成缩略图
                    with cache_lock:  # 仅在访问文件时锁定
                        thumbnail, _, _ = generate_thumbnail(file_path, cache_path)
                    
                    if thumbnail:
                        processed_count += 1
                except Exception as e:
                    logging.error(f"处理图片 {filename} 缓存时出错: {e}")
            
            # 清理不再需要的缓存
            with cache_lock:
                clean_unused_cache()
            
            if processed_count > 0:
                logging.info(f"图片处理完成: 总计处理了 {processed_count}/{total_count} 张图片缓存")
            else:
                logging.info("没有新的缓存生成")
            
            cache_status["processing"] = False
            cache_status["last_update"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        except Exception as e:
            logging.error(f"准备预处理图片时出错: {e}")
            cache_status["processing"] = False
            return

# 监视照片文件夹的函数，在单独的线程中运行
def watch_photos_folder(interval=300):  # 默认5分钟检查一次
    """监视照片文件夹的变化，定期更新缓存"""
    logging.info(f"开始监视照片文件夹，检查间隔: {interval}秒")
    
    global exit_flag
    while not exit_flag:
        try:
            preprocess_all_photos()
            
            # 等待下一次检查，但保持对退出信号的响应
            for _ in range(interval):
                if exit_flag:
                    break
                time.sleep(1)
        except Exception as e:
            logging.error(f"监视照片文件夹时出错: {e}")
            time.sleep(interval)  # 发生错误时，等待下一个间隔

# 获取所有照片信息，使用缓存的缩略图
def get_photos():
    photos = []
    
    # 不在这里预处理所有图片，让后台线程来处理
    # preprocess_all_photos()
    
    for filename in sorted(os.listdir(PHOTOS_FOLDER), key=lambda x: os.path.getmtime(os.path.join(PHOTOS_FOLDER, x)), reverse=True):
        file_path = os.path.join(PHOTOS_FOLDER, filename)
        ext = os.path.splitext(filename)[1].lower()
        
        if os.path.isfile(file_path) and ext in ALLOWED_EXTENSIONS:
            try:
                # 获取文件修改时间
                mod_time = os.path.getmtime(file_path)
                date_modified = datetime.datetime.fromtimestamp(mod_time).strftime('%Y-%m-%d')
                
                # 获取文件大小
                file_size = os.path.getsize(file_path)
                if file_size < 1024 * 1024:
                    size_str = f"{file_size / 1024:.1f} KB"
                else:
                    size_str = f"{file_size / (1024 * 1024):.1f} MB"
                
                # 获取原始图片信息
                with Image.open(file_path) as img:
                    orig_width, orig_height = img.size
                    img_format = img.format
                
                # 使用缓存的缩略图
                cache_filename = get_cache_filename(filename, THUMBNAIL_MAX_SIZE, THUMBNAIL_MAX_SIZE)
                cache_path = os.path.join(THUMBNAIL_FOLDER, cache_filename)
                
                if not os.path.exists(cache_path):
                    # 如果缓存不存在，立即生成缩略图
                    thumbnail, new_width, new_height = generate_thumbnail(file_path, cache_path)
                else:
                    # 如果缓存存在，直接使用
                    with Image.open(cache_path) as thumb:
                        new_width, new_height = thumb.size
                
                # 生成缩略图的URL路径
                thumbnail_url = f"/thumbnails/{cache_filename}"
                
                # 简化文件名显示
                display_name = filename
                if len(display_name) > 25:
                    name_parts = os.path.splitext(display_name)
                    display_name = name_parts[0][:22] + '...' + name_parts[1]
                
                photos.append({
                    'filename': filename,
                    'display_name': display_name,
                    'thumbnail': thumbnail_url,
                    'date': date_modified,
                    'size': size_str,
                    'dimensions': f"{orig_width}x{orig_height}",
                    'width': new_width,
                    'height': new_height,
                    'aspect_ratio': orig_width / orig_height
                })
            except Exception as e:
                logging.error(f"处理图片 {filename} 时出错: {e}")
    
    return photos

# 缓存监控线程
cache_monitor_thread = None

# 启动缓存监控线程的函数
def start_cache_monitor(interval=300):
    global cache_monitor_thread, exit_flag
    
    if cache_monitor_thread is None or not cache_monitor_thread.is_alive():
        exit_flag = False
        cache_monitor_thread = threading.Thread(
            target=watch_photos_folder, 
            args=(interval,),
            daemon=True  # 使用守护线程，这样主程序退出时线程会自动结束
        )
        cache_monitor_thread.start()
        logging.info("缓存监控线程已启动")

# 使用before_request代替before_first_request
@app.before_request
def startup():
    global startup_complete
    if not getattr(app, 'startup_complete', False):
        # 第一次请求时执行，替代before_first_request
        app.startup_complete = True
        
        # 如果不是快速启动模式，在第一个请求时预处理所有图片
        if not FAST_START:
            # 初始化时预处理所有图片
            threading.Thread(target=preprocess_all_photos).start()
        
        # 启动缓存监控线程
        start_cache_monitor()
        logging.info("第一个请求已处理，缓存监控已启动")

@app.route('/')
def index():
    photos = get_photos()
    current_year = datetime.datetime.now().year
    return render_template('index.html', photos=photos, current_year=current_year)

@app.route('/photos/<filename>')
def get_photo(filename):
    return send_from_directory(PHOTOS_FOLDER, filename)

@app.route('/thumbnails/<filename>')
def get_thumbnail(filename):
    return send_from_directory(THUMBNAIL_FOLDER, filename)

@app.route('/api/photos')
def api_photos():
    """API端点，返回所有照片的JSON数据"""
    photos = get_photos()
    return jsonify(photos)

# 手动触发预处理
@app.route('/api/preprocess', methods=['POST'])
def api_preprocess():
    """手动触发预处理所有图片"""
    threading.Thread(target=preprocess_all_photos).start()
    return jsonify({"status": "processing"})

# 缓存状态API
@app.route('/api/cache/status')
def api_cache_status():
    """返回缓存状态信息"""
    global cache_monitor_thread, cache_status
    
    # 计算照片总数和缓存总数
    photo_count = 0
    for filename in os.listdir(PHOTOS_FOLDER):
        if os.path.isfile(os.path.join(PHOTOS_FOLDER, filename)) and \
           os.path.splitext(filename)[1].lower() in ALLOWED_EXTENSIONS:
            photo_count += 1
    
    cache_count = 0
    if os.path.exists(THUMBNAIL_FOLDER):
        cache_count = len(os.listdir(THUMBNAIL_FOLDER))
    
    return jsonify({
        "photo_count": photo_count,
        "cache_count": cache_count,
        "monitor_active": cache_monitor_thread is not None and cache_monitor_thread.is_alive(),
        "cache_folder": THUMBNAIL_FOLDER,
        "processing": cache_status["processing"],
        "progress": {
            "total": cache_status["total"],
            "processed": cache_status["processed"],
            "percent": cache_status["percent"],
            "last_update": cache_status["last_update"]
        }
    })

# 启动/停止缓存监控API
@app.route('/api/cache/monitor/<action>', methods=['POST'])
def api_cache_monitor(action):
    """启动或停止缓存监控"""
    global exit_flag, cache_monitor_thread
    
    if action == 'start':
        interval = request.json.get('interval', 300) if request.is_json else 300
        start_cache_monitor(interval)
        return jsonify({"status": "started", "interval": interval})
    
    elif action == 'stop':
        if cache_monitor_thread and cache_monitor_thread.is_alive():
            exit_flag = True
            # 不要join线程，让它自己结束
            return jsonify({"status": "stopping"})
        else:
            return jsonify({"status": "not_running"})
    
    return jsonify({"error": "invalid action"}), 400

if __name__ == '__main__':
    # 快速启动模式下，不在主线程中执行预处理
    if FAST_START:
        logging.info("应用程序使用快速启动模式，启动完成后将在后台开始预处理...")
    else:
        logging.info("应用程序启动，在后台开始初始预处理...")
        # 在后台线程执行一次预处理
        threading.Thread(target=preprocess_all_photos).start()
    
    # 启动缓存监控线程
    start_cache_monitor()
    
    # 直接启动应用，不等待预处理完成
    app.run(debug=True, host='0.0.0.0',port=5001) 