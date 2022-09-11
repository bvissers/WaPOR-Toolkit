# -*- coding: utf-8 -*-
"""
/***************************************************************************
 WaporDataToolDialog
                                 A QGIS plugin
 This plugin downloads and analyses WaPOR data for a selected region
                             -------------------
        begin                : 2022-07-26
        git sha              : $Format:%H$
        copyright            : (C) 2022 by Brenden & Celray James
        email                : celray@chawanda.com
 ***************************************************************************/

/***************************************************************************
 *                                                                         *
 *   This program is free software; you can redistribute it and/or modify  *
 *   it under the terms of the GNU General Public License as published by  *
 *   the Free Software Foundation; either version 2 of the License, or     *
 *   (at your option) any later version.                                   *
 *                                                                         *
 ***************************************************************************/
"""

import ctypes
import datetime
import os
import time
from pathlib import Path

import numpy as np
import pandas as pd
import requests
from osgeo import gdal, ogr, osr
from PyQt5.QtCore import *
from PyQt5.QtGui import *
from PyQt5.QtWidgets import *

from qgis.core import *
from qgis.gui import QgsMapLayerComboBox
from qgis.PyQt import QtWidgets, uic
from qgis.utils import iface

from qgis.analysis import *

# This loads your .ui file so that PyQt can populate your plugin with the elements from Qt Designer
FORM_CLASS, _ = uic.loadUiType(os.path.join(
     os.path.dirname(__file__), 'waport_data_dialog_base.ui'))



class WaporDataToolDialog(QtWidgets.QDialog, FORM_CLASS):
    def __init__(self, parent=None):
        """Constructor."""
        super(WaporDataToolDialog, self).__init__(parent)
        self.setupUi(self)

        # set constants
        self.path_catalog=r'https://io.apps.fao.org/gismgr/api/v1/catalog/workspaces/'
        self.path_download=r'https://io.apps.fao.org/gismgr/api/v1/download/'
        self.path_jobs=r'https://io.apps.fao.org/gismgr/api/v1/catalog/workspaces/WAPOR/jobs/'
        self.path_query=r'https://io.apps.fao.org/gismgr/api/v1/query/'
        self.path_sign_in=r'https://io.apps.fao.org/gismgr/api/v1/iam/sign-in/'
        self.workspaces='WAPOR_2'

        self.token_is_valid = False

        self.AccessToken = None
        self.time_expire = None
        self.time_start = None

        self.pixel_size = 250
        self.NoData_value = -9999

        self.current_download_location = None


        

        # set initial states
        self.initialise_defaults()

        # connect click functions
        self.treeWidget.itemDoubleClicked.connect(self.LaunchPopup) 
        


        
        self.cbx_workspace.currentTextChanged.connect(self.load_catalog)
        self.btn_update_token.clicked.connect(self.update_token)
        self.btn_check_token.clicked.connect(self.validate_token)
        self.btn_browse_default_download_dir.clicked.connect(self.browse_default_directory)
        self.btn_set_download_location.clicked.connect(self.browse_download_directory)
        self.btn_download.clicked.connect(self.LaunchDownload)

        # analysis functions
        self.btn_browse_txtinout.clicked.connect(self.browse_txtinout_directory)
        self.btn_browse_src_shapefile.clicked.connect(self.browse_hru_shapefile)
        self.btn_analysis_swat_start.clicked.connect(self.analyse_swat)
        self.btn_refresh_dwnl_batch_swat.clicked.connect(self.refresh_swat_wapor_datasets)
        self.btn_analysis_swat_report.clicked.connect(self.createSWATReport)

        # set sizes of items
        self.btn_download.setFixedSize(141, 142)

        self.pbar_primary.setFixedSize(0, 0)
        self.pbar_secondary.setFixedSize(0, 0)

        # attributes for report creation
        self.layout = None
        self.manager = None
        self.project = None


        
    # to store swat constants
    class swat_analysis_var_constants:
        def __init__(self, variable, file_prefix, column_index):
            self.variable = variable
            self.file_prefix = file_prefix
            self.column_index = column_index
    

    def initialise_defaults(self):

        self.txt_default_dir_path.setReadOnly(True)
        self.txb_download_location.setReadOnly(True) 
        # get current date and time and set default time range
        date_time_now = datetime.datetime.now()
        self.date_from.setDate(QDate(2009, 1, 1))
        self.date_to.setDate(QDate(date_time_now))
        
        # set default control state
        self.chb_clip_to_cutline.setCheckState(2)  # it is checked
        
        # set hints
        self.lbl_token_status.setText("")
        self.wapor_tokenbox.setPlaceholderText("Paste your new token here")

        # set default variable states
        self.validate_token()
        self.check_default_download_dir()
        self.check_txtinout_dir()
        self.check_shp_fn()


        # initialise swat analysis defaults
        self.analysis_constants = {}
        self.analysis_constants["swat"] = {}
        self.analysis_constants["swat"]['extraction'] = {}
        self.analysis_constants["swat"]['extraction']['Evapotranspiration'] = self.swat_analysis_var_constants('ET', 'hru_wb_', 14)
        
        self.analysis_constants["swat"]['suffixes'] = {}
        self.analysis_constants["swat"]['suffixes']['Annual Average'] = 'aa'
        self.analysis_constants["swat"]['suffixes']['Annual'] = 'yr'
        self.analysis_constants["swat"]['suffixes']['Decadal (Average)'] = 'day'
        self.analysis_constants["swat"]['suffixes']['Decadal (Cummulative)'] = 'day'


        if self.token_is_valid:
            self.tab_pages.setCurrentIndex(0)
        else:
            self.tab_pages.setCurrentIndex(2)

        default_dir_fn = os.path.join(os.path.dirname(__file__), 'defdir.dll')
        if self.exists(default_dir_fn):
            dld_location = self.read_from(default_dir_fn)[0]
            self.txt_default_dir_path.setText(dld_location)
            self.txb_download_location.setText(dld_location)
            self.current_download_location = dld_location
        else:
            self.txt_default_dir_path.setText("")
            self.txb_download_location.setText("")
            self.current_download_location = None

        self.treeWidget.clear()
        self.treeWidget.headerItem().setText(0, '')
       
        # load catalog to treewidgit
        self.cbx_workspace.setCurrentIndex(25)
        self.load_catalog()
        
        try: self.refresh_swat_wapor_datasets()
        except: pass


    # analysis methods
    
    def analyse_swat(self):

        # set up ui
        root = QgsProject.instance().layerTreeRoot()
        waporTkGroup = root.findGroup("Wapor Toolkit")
        
        if waporTkGroup is None:
            waporTkGroup = root.insertGroup(0, "Wapor Toolkit")

        swat_analysis = waporTkGroup.findGroup("SWAT+ Analysis")

        if swat_analysis is None:
            swat_analysis = waporTkGroup.insertGroup(0, "SWAT+ Analysis")
        
        hru_layer = self.get_layer("HRU Shapefile", "SWAT+ Analysis")

        if hru_layer is None:
            vlayer = QgsVectorLayer(self.lnEdit_hru.text(), 'HRU Shapefile', 'ogr')
            self.loadLayerToGroup(vlayer, 'SWAT+ Analysis')

        hru_layer = self.get_layer("HRU Shapefile", "SWAT+ Analysis")

        # check capability to add field and add if possible
        capabilities = hru_layer.dataProvider().capabilities()
        if capabilities & QgsVectorDataProvider.AddAttributes:
            # check if field exists before adding
            field_index = hru_layer.fields().indexFromName('VarValue')

            if field_index == -1:
                res = hru_layer.dataProvider().addAttributes([QgsField('VarValue', QVariant.Double)])
                hru_layer.updateFields()
                print(f"The field 'VarValue' has been added to layer {hru_layer.name()}!")
            else: 
                print(f"The field 'VarValue' exists in layer {hru_layer.name()}!")
        else:
            print("HRU Shapefile is not editable!")

        # edit layer by filling with corresponding variable values

        # get values in dictionary form
        # read file

        fn_prefix = self.analysis_constants['swat']['extraction'][self.cbx_analysis_swat_var.currentText()].file_prefix
        c_index = self.analysis_constants['swat']['extraction'][self.cbx_analysis_swat_var.currentText()].column_index
        fn_suffix = self.analysis_constants["swat"]['suffixes'][self.cbx_analysis_swat_ts.currentText()]

        read_fn = f"{self.lnEdit_txtinout.text()}/{fn_prefix}{fn_suffix}.txt"

        if self.exists(read_fn):
            hru_fc = self.read_from(read_fn)
            if len(hru_fc) < 5:
                
                # here, tell user to run at specified timestep in SWAT+ again to get
                # results making sure output for the variable and timestep is activated
                return
            
            hru_var_values = {}

            for line in hru_fc[3:]:
                line = line.split(" ")
                line = [element for element in line if not element == ""]
                year = line[3]

                if not year in hru_var_values:
                    hru_var_values[year] = {}

                hru_var_values[year][int(line[4])] = float(line[c_index])

            # add to shapefile to make raster files
            if fn_suffix == 'aa':
                    
                hru_layer.startEditing()

                hru_layer_features = hru_layer.getFeatures()

                for feature in hru_layer_features:
                    feature["VarValue"] = hru_var_values[year][int(feature["HRUS"])]
                    print(feature["VarValue"])
                    hru_layer.updateFeature(feature)

                hru_layer.commitChanges()
                iface.vectorLayerTools().stopEditing(hru_layer)

                # rasterise the layer

                self.create_path(f"{self.txb_download_location.text()}/swat-plus/")
                rasterised = self.rasterise_layer(
                    self.lnEdit_hru.text(),
                    f"{self.txb_download_location.text()}/swat-plus/{fn_suffix}_{self.analysis_constants['swat']['extraction'][self.cbx_analysis_swat_var.currentText()].variable}.tif",
                    'VarValue')

                # add it to canvas
                if rasterised:
                    swat_rasters = swat_analysis.findGroup("Raster Layers")
                    if swat_rasters is None:
                        swat_rasters = swat_analysis.insertGroup(0, "Raster Layers")

                    aa_rasters = swat_rasters.findGroup("Annual Average")
                    if aa_rasters is None:
                        aa_rasters = swat_rasters.insertGroup(0, "Annual Average")

                    # rasterised_aa_layer = self.get_layer(
                    #         f"Annual average {self.analysis_constants['swat']['extraction'][self.cbx_analysis_swat_var.currentText()].variable}",
                    #         "Raster Layers"
                    #     )
                    
                    self.removeLayers(f"SWAT+ {fn_suffix} {self.analysis_constants['swat']['extraction'][self.cbx_analysis_swat_var.currentText()].variable}")

                    rlayer = QgsRasterLayer(
                        f"{self.txb_download_location.text()}/swat-plus/{fn_suffix}_{self.analysis_constants['swat']['extraction'][self.cbx_analysis_swat_var.currentText()].variable}.tif",
                        f"SWAT+ {fn_suffix} {self.analysis_constants['swat']['extraction'][self.cbx_analysis_swat_var.currentText()].variable}", 'gdal')

                    self.loadLayerToGroup(rlayer, 'Annual Average')
                    
                    # process annual average for WaPOR side

                    # check the WaPOR raster if it exists
                    
                    # Dictionaries to turn cbb selections to cubecode strings
                    Dic_type = {'Evaporation' : 'E', 'Evaporation' : 'T',
                            'Precipitation' : 'PCP', 'Evapotranspiration' : 'AETI'}
                    Dic_time = {'Annual Average' : 'A', 'Annual' : 'A', 'Monthly' : 'M',
                            'Decadal (Average)' : 'D', 'Decadal (Cumulative)' : 'D_C'}

                    # NEED TO ADD MAKE self.cbx_analysis_lvl.currentText() for L1, L2, L3
                    level = self.cbx_analysis_lvl.currentText()
                    rootdir = f"{self.txb_download_location.text()}/{self.cbb_download_batch_swat.currentText()}"
                    parameter = (Dic_type[self.cbx_analysis_swat_var.currentText()] + '_' +
                                          Dic_time[self.cbx_analysis_swat_ts.currentText()])

                    output_file = ""
                    for folder in os.listdir(rootdir):
                        if (level in folder) and (parameter in folder):
                            #Gives full file extention of where the rasterfiles are located
                            
                            print(os.path.join(rootdir , folder))
                            
                            # proceed to average calculations

                            # todo: add warning if date range is not valid, wise to default to available data
                            analysis_start = self.date_from_swat.date().toPyDate()
                            analysis_end = self.date_to_swat.date().toPyDate()

                            self.create_path(f"{self.txb_download_location.text()}/wapor/")
                            output_file = f"{self.txb_download_location.text()}/wapor/{fn_suffix}_{self.analysis_constants['swat']['extraction'][self.cbx_analysis_swat_var.currentText()].variable}.tif"
                            self.raster_mean(os.path.join(rootdir , folder), analysis_start, analysis_end, output_file)
                            
                        else:
                            pass
                            # warn  user that the needed files were not found and prompt a download for the specified period (or just suggest)
                    
                    
                    if self.exists(output_file):
                        rlayer = QgsRasterLayer(
                            f"{output_file}", f"WaPOR {fn_suffix} AET", 'gdal')
                        
                        self.loadLayerToGroup(rlayer, 'Annual Average')
                    
    def createSWATReport(self):
        print('creating report')
        # prepare a blank layout
        fn_suffix   = self.analysis_constants["swat"]['suffixes'][self.cbx_analysis_swat_ts.currentText()]
        var_name    = self.analysis_constants['swat']['extraction'][self.cbx_analysis_swat_var.currentText()].variable

        self.project = QgsProject.instance()         
        self.manager = self.project.layoutManager()

        # incase the layout already exists
        layoutName = f"SWAT+ {fn_suffix} {var_name} Report"

        layouts_list = self.manager.printLayouts()
        for layout in layouts_list:
            if layout.name() == layoutName:
                self.manager.removeLayout(layout)

        self.layout = None
        self.layout = QgsPrintLayout(self.project)        

        self.layout.initializeDefaults()  
        
        self.layout.setName(layoutName)
        self.manager.addLayout(self.layout)

        # add difference map here
        sw_raster = QgsProject.instance().mapLayersByName('SWAT+ aa ET')[0]
        swat_target=QgsRasterCalculatorEntry()
        swat_target.raster = sw_raster
        swat_target.bandNumber = 1
        swat_target.ref = 'swat_raster@1'

        wp_raster = QgsProject.instance().mapLayersByName('WaPOR aa AET')[0]
        wapor_target=QgsRasterCalculatorEntry()
        wapor_target.raster = wp_raster
        wapor_target.bandNumber = 1
        wapor_target.ref = 'wapor_raster@1'

        entries = [ swat_target , wapor_target ]

        final_output = f"{self.txb_download_location.text()}/swat-plus/{fn_suffix}_{self.analysis_constants['swat']['extraction'][self.cbx_analysis_swat_var.currentText()].variable}_diff.tif"
        
        calc=QgsRasterCalculator ( f'{wapor_target.ref} - {swat_target.ref}', f"{final_output}" , 'GTiff', sw_raster.extent(), sw_raster.width(), sw_raster.height(), entries )
        calc.processCalculation()

        lyr_nm_tmp = f"{self.cbx_analysis_swat_var.currentText()} Difference (WaPOR - SWAT+)"
        final_raster=QgsRasterLayer(final_output, lyr_nm_tmp, "gdal")
        
        self.loadLayerToGroup(final_raster, 'Annual Average')

        # populate the layout
                                          

        #defines map extent using map coordinates
        # SWAT Layer first

        current_layer = None
        for tmp_layer in QgsProject.instance().mapLayers().values():
            if tmp_layer.name() == 'WaPOR aa AET':
                current_layer = tmp_layer
        
        layout_map_extents = self.transformExtents(current_layer.extent(), current_layer.crs())

        swat_map = QgsLayoutItemMap(self.layout)
        swat_map.setRect(20, 20, 20, 20)   

        swat_map.setLayers([QgsProject.instance().mapLayersByName('SWAT+ aa ET')[0]])
        swat_map.storeCurrentLayerStyles()
        swat_map.setExtent(layout_map_extents)
        self.layout.addLayoutItem(swat_map)
        
        #Move & Resize map on print layout canvas
        swat_map.attemptMove(QgsLayoutPoint(30, 30, QgsUnitTypes.LayoutMillimeters))
        swat_map.attemptResize(QgsLayoutSize(80, 80, QgsUnitTypes.LayoutMillimeters))

        # Wapor Layer now
        wapor_map = QgsLayoutItemMap(self.layout)
        wapor_map.setRect(20, 20, 20, 20)

        wapor_map.setLayers([QgsProject.instance().mapLayersByName('WaPOR aa AET')[0]])
        wapor_map.storeCurrentLayerStyles()
        wapor_map.setExtent(layout_map_extents)
        self.layout.addLayoutItem(wapor_map)

        #Move & Resize map on print layout canvas
        wapor_map.attemptMove(QgsLayoutPoint(115, 30, QgsUnitTypes.LayoutMillimeters))
        wapor_map.attemptResize(QgsLayoutSize(80, 80, QgsUnitTypes.LayoutMillimeters))

        # Difference map layer now
        diff_map_layer = QgsLayoutItemMap(self.layout)
        diff_map_layer.setRect(20, 20, 20, 20)

        diff_map_layer_ = QgsProject.instance().mapLayersByName(lyr_nm_tmp)[0]

        diff_map_layer.setLayers([diff_map_layer_])
        diff_map_layer.storeCurrentLayerStyles()
        diff_map_layer.setExtent(layout_map_extents)
        self.layout.addLayoutItem(diff_map_layer)

        #Move & Resize map on print layout canvas
        diff_map_layer.attemptMove(QgsLayoutPoint(200, 30, QgsUnitTypes.LayoutMillimeters))
        diff_map_layer.attemptResize(QgsLayoutSize(80, 80, QgsUnitTypes.LayoutMillimeters))

        # style layers
        raster_layers_aa_ET = [
            QgsProject.instance().mapLayersByName('SWAT+ aa ET')[0],
            QgsProject.instance().mapLayersByName('WaPOR aa AET')[0],
        ]

        et_style_fn = os.path.join(os.path.dirname(__file__), 'et_style.qml')
        et_dif_style_fn = os.path.join(os.path.dirname(__file__), 'et_diff_style.qml')

        # load layer styles
        diff_map_layer_.loadNamedStyle(et_dif_style_fn)
        iface.layerTreeView().refreshLayerSymbology(diff_map_layer_.id())
        diff_map_layer_.triggerRepaint()

        for rast_lyr in raster_layers_aa_ET:
            rast_lyr.loadNamedStyle(et_style_fn)
            iface.layerTreeView().refreshLayerSymbology(rast_lyr.id())
            rast_lyr.triggerRepaint()

        # add legends
        legend = QgsLayoutItemLegend(self.layout)
        # legend_diff = QgsLayoutItemLegend(self.layout)
        legend.setTitle("")
        # legend_diff.setTitle("")

        layersToAdd = [layer for layer in QgsProject().instance().mapLayers().values() if layer.name() == 'WaPOR aa AET']
        # layersToAdd_diff = [layer for layer in QgsProject().instance().mapLayers().values() if layer.name() == lyr_nm_tmp]

        root = QgsLayerTree()
        root_dif = QgsLayerTree()

        for layer in layersToAdd:
            #add layer objects to the layer tree
            root.addLayer(layer)

        for layer_diff in layersToAdd_diff:
            #add layer objects to the layer tree
            root_dif.addLayer(layer_diff)

        legend.model().setRootGroup(root)
        self.layout.addLayoutItem(legend)
        legend.attemptMove(QgsLayoutPoint(40, 110, QgsUnitTypes.LayoutMillimeters))
        

        # legend_diff.model().setRootGroup(root)
        # self.layout.addLayoutItem(legend_diff)
        # legend_diff.attemptMove(QgsLayoutPoint(210, 110, QgsUnitTypes.LayoutMillimeters))
        



        # add text
        title = QgsLayoutItemLabel(self.layout)
        title.setText("Annual Average ET Report")
        title.setFont(QFont("Arial", 24))
        title.adjustSizeToText()
        self.layout.addLayoutItem(title)
        title.attemptMove(QgsLayoutPoint(10, 10, QgsUnitTypes.LayoutMillimeters))
        title.attemptResize(QgsLayoutSize(1100, 80, QgsUnitTypes.LayoutMillimeters))

        subtitle = QgsLayoutItemLabel(self.layout)
        subtitle.setText("SWAT+ ET vs WaPOR Actual ET and Interception")
        subtitle.setFont(QFont("Arial", 16))
        subtitle.adjustSizeToText()
        self.layout.addLayoutItem(subtitle)
        subtitle.attemptMove(QgsLayoutPoint(10, 20, QgsUnitTypes.LayoutMillimeters)) 
        
        comparison_details = QgsLayoutItemLabel(self.layout)
        comparison_details.setText("Comparison Details")
        comparison_details.setFont(QFont("Arial", 16))
        comparison_details.adjustSizeToText()
        self.layout.addLayoutItem(comparison_details)
        comparison_details.attemptMove(QgsLayoutPoint(100, 110, QgsUnitTypes.LayoutMillimeters)) 
        comparison_details.attemptResize(QgsLayoutSize(1100, 80, QgsUnitTypes.LayoutMillimeters))


















        # now we save where the user wants to save the output pdf file, commented out image file
        save_report_fn = QFileDialog.getSaveFileName(None, 'Select HRU (hru2.shp) shapefile', '/', "PDF File (*.pdf)")
        if (not save_report_fn is None):
            if (len(save_report_fn[0]) > 4):
                self.exportLayout(f"SWAT+ {fn_suffix} {var_name} Report", save_report_fn[0], self.manager)
        return True


    def transformExtents(self, extentObject, src_crs, dest_crs_EPSG = '4326'):
        dst_crs = QgsCoordinateReferenceSystem(f"EPSG:{dest_crs_EPSG}")
        transform_context = QgsProject.instance().transformContext()
        transformer = QgsCoordinateTransform(src_crs, dst_crs, transform_context)
        return transformer.transformBoundingBox(extentObject)


    def layer_visibility(self, layerName, visible = False, includes_parent = False):
        prj = QgsProject.instance()
        layer = prj.mapLayersByName(layerName)[0]
        if includes_parent:
            prj.layerTreeRoot().findLayer(layer.id()).setItemVisibilityChecked(visible)
        else:
            prj.layerTreeRoot().findLayer(layer.id()).setItemVisibilityCheckedParentRecursive(visible)



    def exportLayout(self, layoutName, fileOut, exportManager):

        #this accesses a specific layout, by name (which is a string)
        layout = exportManager.layoutByName(layoutName)
        
        #this creates a QgsLayoutExporter object and this exports a pdf of the layout object
        exporter = QgsLayoutExporter(layout)                
        exporter.exportToPdf(fileOut, QgsLayoutExporter.PdfExportSettings())  

        #this exports an image of the layout object but is currently deactivated
        #exporter.exportToImage('/Users/ep9k/Desktop/TestLayout.png', QgsLayoutExporter.ImageExportSettings())  






    def write_raster(self, raster_array, gt, data_obj, outputpath, dtype, nodata, nbands=1):

        height, width = raster_array.shape

        # Prepare destination file
        driver = gdal.GetDriverByName("GTiff")
        dest = driver.Create(outputpath, width, height, nbands, dtype)

        # Write output raster
        dest.GetRasterBand(1).WriteArray(raster_array)
        dest.GetRasterBand(1).SetNoDataValue(nodata)
        
        # Set transform and projection
        dest.SetGeoTransform(gt)
        wkt = data_obj.GetProjection()
        srs = osr.SpatialReference()
        srs.ImportFromWkt(wkt)
        dest.SetProjection(srs.ExportToWkt())

        # Close output raster dataset 
        dest = None

    def raster_mean(self, wapor_analysis_folder, start, end, out_fn):   
            
        Filelist = []
        for file in os.listdir(wapor_analysis_folder):
            try: 
                substring_i = file.find('[')
                Filedate = datetime.datetime.strptime(file[substring_i + 1 : substring_i + 11],'%Y-%m-%d').date()
                #Start is inclusive, End is exclusive

                # this did not work because of diffferences in object types
                if Filedate >= start and Filedate < end:
                    Filelist.append(os.path.join(wapor_analysis_folder,file))
            except:
                raise
                
        i=0
        print(Filelist)
        allarrays = None
        for file in Filelist:
            print(file)
            print(file)
            print(file)
            print(file)
            print(file)
            i +=1
            gd_obj = gdal.Open(file)
            array = gd_obj.ReadAsArray()
            array = np.expand_dims(array,2)
            
            if i == 1:
                allarrays = array
                
                srs = gdal.Open(file)
                srs.RasterCount
                nodata = (srs.GetRasterBand(1).GetNoDataValue())
                srs = None
                
            else:
                allarrays = np.concatenate((allarrays, array), axis=2)

        #currently doesn't exclude locations with a mix of nodata values and real values
        mean_of_tiffs = np.nanmean(allarrays, axis=2)
        
        #Currently save the mean raster with the rest of the other files
        self.write_raster(mean_of_tiffs, gd_obj.GetGeoTransform(), gd_obj, out_fn, gdal.GDT_Float32, nodata)
        




















    def loadLayerToGroup(self, layer, group_name):
        QgsProject.instance().addMapLayer(layer, False)
        root = QgsProject.instance().layerTreeRoot()
        g = root.findGroup(group_name)
        g.insertChildNode(0, QgsLayerTreeLayer(layer))


    def removeLayers(self, layerName):
        for layer in QgsProject.instance().mapLayers().values():
            if layer.name()==layerName:
                QgsProject.instance().removeMapLayers( [layer.id()] )




    def rasterise_layer(self, vector_fn, raster_fn, field_name, pixel_size = None, NoData_value = None):
        
        if pixel_size is None:
            pixel_size = self.pixel_size
            
        if NoData_value is None:
            NoData_value = self.NoData_value

        # Open the data source and read in the extent
        source_ds = ogr.Open(vector_fn)
        source_layer = source_ds.GetLayer()
        x_min, x_max, y_min, y_max = source_layer.GetExtent()

        # Create the destination data source
        x_res = int((x_max - x_min) / pixel_size)
        y_res = int((y_max - y_min) / pixel_size)
        target_ds = gdal.GetDriverByName('GTiff').Create(raster_fn, x_res, y_res, 1, gdal.GDT_Float32)
        target_ds.SetGeoTransform((x_min, pixel_size, 0, y_max, 0, -pixel_size))
        target_ds.SetProjection(source_layer.GetSpatialRef().ExportToWkt())
        band = target_ds.GetRasterBand(1)
        band.SetNoDataValue(NoData_value)

        # Rasterize
        OPTIONS = [f'ATTRIBUTE={field_name}']
        gdal.RasterizeLayer(target_ds, [1], source_layer, burn_values=[0], options=OPTIONS)
        return True


    def create_path(self, path_name):

        if not os.path.isdir(path_name):
            os.makedirs(path_name)
                   
        return path_name


    def refresh_swat_wapor_datasets(self):
        directories = os.listdir(self.txb_download_location.text())
        self.cbb_download_batch_swat.clear()

        for dir_name in directories:
            if 'WaPOR Download' in dir_name:
                self.cbb_download_batch_swat.addItem(dir_name)


    def get_layer(self, layer_name, group_name):
        root = QgsProject.instance().layerTreeRoot()
        group = root.findGroup(group_name)
        selected_layer = None
        if group is not None:
            for child in group.children():
                if child.name() == layer_name:
                    iface.setActiveLayer(child.layer())
                    selected_layer = child.layer()

        return selected_layer
                


    def load_catalog(self):
        print('loading catalog...')
        
        
        if self.cbx_workspace.currentText() == 'WAPOR_2':
            print(2)    
            
            try:
                #Cubes: provides operations pertaining to Cube resources
                #returns a list of available Cube resource type items
                #example:https://io.apps.fao.org/gismgr/api/v1/catalog/workspaces/WAPOR_2/cubes?overview=false&paged=false&sort=sort%20%3D%20code&tags=L1'
                L1 = ((requests.get('{0}{1}/cubes?overview=false&paged=false&sort=sort%20%3D%20code&tags=L1'.format(self.path_catalog,self.workspaces)).json()).get('response'))
                L2 = ((requests.get('{0}{1}/cubes?overview=false&paged=false&sort=sort%20%3D%20code&tags=L2'.format(self.path_catalog,self.workspaces)).json()).get('response'))
                L3 = ((requests.get('{0}{1}/cubes?overview=false&paged=false&sort=sort%20%3D%20code&tags=L3'.format(self.path_catalog,self.workspaces)).json()).get('response'))            
                
                
                #sorts by type of information
                L1 = sorted(L1, key=lambda d: d.get('caption'))
                L2 = sorted(L2, key=lambda d: d.get('caption'))
                
                #Sorts first country location, then by type of information
                L3 = sorted(L3, key=lambda d: [d.get('additionalInfo', {}).get('spatialExtent').partition(", ")[2],d.get('additionalInfo', {}).get('spatialExtent').partition(", ")[1] ,d.get('code') ])
                
                self.MasterList = L1 + L2 + L3
                level_list = [L1, L2, L3] 
                #creates the treewidget to select data from
                self.treeWidget.clear()
                self.treeWidget.headerItem().setText(0, self.workspaces)
                self.TreeWaPOR(level_list, "WAPOR_2")
                
                self.combo_dekadal.show()
                self.label_dekadal.show()
                
                
            except: pass
        
        else:
            try:
                L = ((requests.get('{0}{1}/cubes?overview=false&paged=false'.format(self.path_catalog,self.cbx_workspace.currentText())).json()).get('response'))
                self.MasterList = L
                self.treeWidget.clear()
                self.treeWidget.headerItem().setText(0, self.cbx_workspace.currentText())
                self.TreeAddBasic(L, self.cbx_workspace.currentText())    
                
                self.combo_dekadal.hide()
                self.label_dekadal.hide()
            except: pass
            # self.Mbox( 'Error' ,'Error Loading data from the WaPOR Server',0)





    def TreeAddBasic(self, L, name):    
        parent = QTreeWidgetItem(self.treeWidget)

        parent.setText(0, name)        
        parent.setFlags(parent.flags()   | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsAutoTristate)
        
        for x in L:
            child = QTreeWidgetItem(parent)
            child.setFlags(child.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            child.setText(0, x.get('caption'))
            child.setText(1, str(x.get('code')))
            child.setCheckState(0, Qt.CheckState.Unchecked)
            
            
    def TreeWaPOR(self, level_list, name):    
        parent = QTreeWidgetItem(self.treeWidget)
        parent.setText(0, name)
        parent.setFlags(parent.flags() |  Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsAutoTristate)
        Levels = ['Level 1', 'Level 2', 'Level 3']
        locations = []
        index = 0
        for L in level_list:        
            level = QTreeWidgetItem(parent)
            level.setFlags(level.flags() | Qt.ItemFlag.ItemIsUserCheckable| Qt.ItemFlag.ItemIsAutoTristate)
            level.setText(0, Levels[index])
            level.setCheckState(0, Qt.CheckState.Unchecked)
                       
            
            
            if L != level_list[2]:
                for x in L:
                    child = QTreeWidgetItem(level)
                    child.setFlags(child.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    child.setText(0, x.get('caption'))
                    child.setText(1, str(x.get('code')))
                    child.setCheckState(0, Qt.CheckState.Unchecked)
   
            if L == level_list[2]:
                for x in L:
                    if (x.get('additionalInfo').get('spatialExtent')) not in locations:
                        locations.append(x.get('additionalInfo').get('spatialExtent'))
                        country = QTreeWidgetItem(level)
                        country.setFlags(country.flags() | Qt.ItemFlag.ItemIsUserCheckable| Qt.ItemFlag.ItemIsAutoTristate)
                        country.setText(0, locations[-1])
                        country.setCheckState(0, Qt.CheckState.Unchecked)        
        
                    child2 = QTreeWidgetItem(country)
                    child2.setFlags(child2.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    child2.setText(0, x.get('caption'))
                    child2.setCheckState(0, Qt.CheckState.Unchecked)     
                    child2.setText(1, str(x.get('code')))              
            index += 1

            
    def get_bbox(self):
        vector_layer = self.mMapLayerComboBox.currentLayer()
        bounding=vector_layer.extent()   
        if vector_layer.crs() != QgsCoordinateReferenceSystem("EPSG:4326"):
            crsSrc = vector_layer.crs()    # WGS 84
            crsDest = QgsCoordinateReferenceSystem("EPSG:4326")# WGS 84 / UTM zone 33N
            transformContext = QgsProject.instance().transformContext()
            xform = QgsCoordinateTransform(crsSrc, crsDest, transformContext)
            bounding_t = xform.transformBoundingBox(bounding)
            bounding = bounding_t
            
        bbox = [bounding.xMinimum(),bounding.yMinimum(),bounding.xMaximum(),bounding.yMaximum()]
        return bbox


    def LaunchDownload(self):
        start_date = self.date_from.date().toPyDate().strftime("%Y-%m-%d")
        end_date = self.date_to.date().toPyDate().strftime("%Y-%m-%d")
        polygon_name = self.mMapLayerComboBox.currentLayer()
        vector_extent = polygon_name.extent()
        vector_location = polygon_name.dataProvider().dataSourceUri()
        bbox = self.get_bbox()
        token = self.read_token()
        
        self.worker = WorkerThread(token, bbox, 
                                   self.current_download_location, self.chb_clip_to_cutline.isChecked(), self.combo_dekadal.currentText(),
                                   self.treeWidget, start_date, end_date, self.MasterList, vector_location, self.cbx_workspace.currentText())
        self.worker.start()
        self.worker.UpdateStatus.connect(self.evt_UpdateStatusUI)
        self.worker.UpdateProgress.connect(self.UpdateProgressUI)
        
        # if download is in progress, let us cancel
        # if not let us launch and we will change text in any case
        if self.btn_download.text() == "Retrieve Data":
            self.btn_download.setText("Cancel Download")
            self.btn_download.clicked.disconnect(self.LaunchDownload)
            self.btn_download.clicked.connect(self.StopDownload)

            self.btn_download.setFixedSize(141, 101)
            
            self.pbar_primary.setFixedSize(141, 9)
            self.pbar_secondary.setFixedSize(141, 21)

            self.pbar_primary.setValue(0)
            self.pbar_secondary.setValue(0)
            

            
        
    def evt_UpdateStatusUI(self, text):
        if text == "Status: Download Completed":
            if self.btn_download.text() == "Cancel Download":
                self.btn_download.setFixedSize(141, 142)

                self.pbar_primary.setFixedSize(0, 0)
                self.pbar_secondary.setFixedSize(0, 0)

                self.btn_download.setText("Retrieve Data")
                self.btn_download.clicked.disconnect(self.StopDownload)
                self.btn_download.clicked.connect(self.LaunchDownload)

        self.labelStatus.setText(text)
    
    
    def UpdateProgressUI(self, text):

        if len(text.split("\n")) == 3:
            # we use try so that if FAO gives an update and this routine breaks, the plugin is still usable
            try:
                # update progress bar
                base_parts = text.split("\n"); cpp = base_parts[1].split(' '); spp = base_parts[2].split(' ')

                current_primary_progress = [int(cpp[2]), int(cpp[4])]
                current_secondary_progress = [int(spp[2]), int(spp[4])]

                print(current_primary_progress)
                print(current_secondary_progress)

                self.pbar_primary.setMaximum(current_primary_progress[1])
                self.pbar_primary.setValue(current_primary_progress[0])

                self.pbar_secondary.setMaximum(current_secondary_progress[1])
                self.pbar_secondary.setValue(current_secondary_progress[0])

            except: pass
        
        self.labelProgress.setText(text)
        
        
    def StopDownload(self):
        self.worker.requestInterruption()
        self.labelStatus.setText("Status: Canceling Download")  

        
        if self.btn_download.text() == "Cancel Download":
            self.btn_download.setFixedSize(141, 142)

            self.pbar_primary.setFixedSize(0, 0)
            self.pbar_secondary.setFixedSize(0, 0)

            self.btn_download.setText("Retrieve Data")
            self.btn_download.clicked.disconnect(self.StopDownload)
            self.btn_download.clicked.connect(self.LaunchDownload)
        
    def update_token(self):
        print('updating token...')
        token_fn = os.path.join(os.path.dirname(__file__), 'token.dll')
        
        token_text = self.wapor_tokenbox.toPlainText()

        self.write_to(token_fn, token_text)


    def read_token(self):
        print('reading token...')
        token_fn = os.path.join(os.path.dirname(__file__), 'token.dll')
        
        if self.exists(token_fn): return self.read_from(token_fn)[0]
        else: return ''


    def validate_token(self):
        resp_vp=requests.post(self.path_sign_in,headers={'X-GISMGR-API-KEY':self.read_token()})
        resp_vp = resp_vp.json()
        print(resp_vp)
        try:
            if resp_vp['message'] == "OK":
                self.token_is_valid = True
                self.lbl_token_status.setText("Current Token is OK")

                self.AccessToken = resp_vp['response']['accessToken']
                self.time_expire = resp_vp['response']['expiresIn']
                self.time_start = datetime.datetime.now().timestamp()

        except:
            self.lbl_token_status.setText("Can't Validate Token")
            print( 'Error','Could not connect to server.',0)  

    def browse_default_directory(self):
        print('select default directory...')

        startingDir = "/"
        destDir = QFileDialog.getExistingDirectory(None, 
                                                    'Select Default Download Directory', 
                                                    startingDir, 
                                                    QFileDialog.ShowDirsOnly)

        if len(destDir) > 0:
            default_dir_fn = os.path.join(os.path.dirname(__file__), 'defdir.dll')
            self.write_to(default_dir_fn, destDir)
            self.check_default_download_dir()
        
        
    def browse_download_directory(self):
        print('select download directory...')

        startingDir = "/"
        destDir = QFileDialog.getExistingDirectory(None, 
                                                    'Select Download Directory', 
                                                    startingDir, 
                                                    QFileDialog.ShowDirsOnly)

        if len(destDir) > 0:
            self.txb_download_location.setText(destDir)
            self.current_download_location = destDir
            
        
        
    def browse_txtinout_directory(self):
        print('select txtinout directory...')

        startingDir = "/"

        
        destDir = QFileDialog.getExistingDirectory(None, 
                                                'Select TxtInOut Directory for SWAT+ model', 
                                                startingDir, 
                                                QFileDialog.ShowDirsOnly)

        if len(destDir) > 0:
            txtinout_fn = os.path.join(os.path.dirname(__file__), 'txtdir.dll')
            self.write_to(txtinout_fn, destDir)
            self.check_txtinout_dir()
        
        
    def browse_hru_shapefile(self):
        print('select shapefile...')

        startingDir = "/"

        destFN = QFileDialog.getOpenFileName(
            None, 'Select HRU (hru2.shp) shapefile', startingDir, "Shapefile (*.shp)")

        destFN = destFN[0]
        if len(destFN) > 0:
            hru_shp_fn = os.path.join(os.path.dirname(__file__), 'shp.dll')
            self.write_to(hru_shp_fn, destFN)
            self.check_shp_fn()

            
        
    def check_shp_fn(self):
        hru_shp_fn = os.path.join(os.path.dirname(__file__), 'shp.dll')
        if self.exists(hru_shp_fn):
            if len(self.read_from(hru_shp_fn)) > 0:
                self.lnEdit_hru.setText(self.read_from(hru_shp_fn)[0])

        
    def check_default_download_dir(self):
        default_dir_fn = os.path.join(os.path.dirname(__file__), 'defdir.dll')
        if self.exists(default_dir_fn):
            self.txt_default_dir_path.setText(self.read_from(default_dir_fn)[0])
        else:
            self.txt_default_dir_path.setText("")

        
    def check_txtinout_dir(self):
        txtinout_fn = os.path.join(os.path.dirname(__file__), 'txtdir.dll')
            
        if self.exists(txtinout_fn):
            txtinout_path = self.read_from(txtinout_fn)[0]
            if self.exists(f"{txtinout_path}/file.cio"):
                self.lnEdit_txtinout.setText(txtinout_path)
                
                # load hru shapefile if standard SWAT+ model
                if self.exists(f"{txtinout_path}/../../../Watershed/Shapes/hrus2.shp"):
                    self.lnEdit_hru.setText(os.path.abspath(f"{txtinout_path}/../../../Watershed/Shapes/hrus2.shp").replace("\\", "/"))
            else:
                self.lnEdit_txtinout.setText('Invalid TxtInOut Directory')
                self.lnEdit_hru.setText("")
        else:
            self.lnEdit_txtinout.setText("")


    def write_to(self, filename, text_to_write):
        '''
        a function to write to file
        '''
        if not os.path.isdir(os.path.dirname(filename)): os.makedirs(os.path.dirname(filename))
        g = open(filename, 'w', encoding="utf-8")
        g.write(text_to_write)
        g.close()


    def exists(self, path_):
        if os.path.isdir(path_): return True
        if os.path.isfile(path_): return True
        return False


    def read_from(self, filename):
        '''
        a function to read ascii files
        '''
        g = open(filename, 'r')
        file_text = g.readlines()
        g.close
        return file_text
    

    def LaunchPopup(self, item):
        if item.childCount()  == 0 or item.parent() == None:
            if item.childCount()  == 0 and item.parent() != None:
                self.pop = InfoPopup(item.text(1), 0, self.MasterList)
            if item.parent() == None:
                self.pop = InfoPopup(item.text(0),1)
            self.pop.setWindowTitle(item.text(0))
            self.pop.show()
        

class InfoPopup(QWidget):
        def __init__(self, code, index, MasterList = None):
            super().__init__()
            layout = QGridLayout()
            if index == 0:
                for x in MasterList:
                    if str(x.get('code'))  == code:                 
                      keylist = ['caption', 'code', 'description']+list(x.get('additionalInfo').keys())
                      valuelist = [x.get('caption'), x.get('code'), x.get('description')]+list(x.get('additionalInfo').values())
                      
                      keyitems = len(keylist)-1
                      keyposition = 0
                      while keyposition <= keyitems:
                              y = QLabel(str(keylist[keyposition]))
                              y.setWordWrap(True)
                              y.setStyleSheet("border: 1px solid black;")
                              y.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                              layout.addWidget(y,keyposition,0)
                                
                              y = QLabel(str(valuelist[keyposition]))
                              y.setWordWrap(True)
                              y.setStyleSheet("border: 1px solid black;")
                              y.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                              layout.addWidget(y,keyposition,1)
                              
                              keyposition += 1
                      break         
            if index == 1:
                      responce = ((requests.get(r'https://io.apps.fao.org/gismgr/api/v1/catalog/workspaces/{0}'.format(code)).json()).get('response'))
                      print(responce)
                      keylist = []
                      valuelist = []
                      try:
                          valuelist.append(responce['description'])
                          keylist.append(responce['caption'])
                      except:
                          pass
                      try:
                          valuelist.append(responce['additionalInfo']['created'])
                          keylist.append('Created')
                      except:
                          pass
                      try:
                          valuelist.append(responce['additionalInfo']['site'])
                          keylist.append('Site')
                      except:
                          pass
                        
                      
                      keyitems = len(keylist)-1
                      keyposition = 0
                      while keyposition <= keyitems:
                              y = QLabel(str(keylist[keyposition]))
                              y.setWordWrap(True)
                              y.setStyleSheet("border: 1px solid black;")
                              y.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                              layout.addWidget(y,keyposition,0)
                                
                              y = QLabel(str(valuelist[keyposition]))
                              y.setWordWrap(True)
                              y.setStyleSheet("border: 1px solid black;")
                              y.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                              layout.addWidget(y,keyposition,1)
                              
                              keyposition += 1
                               
            
            
            
            self.setLayout(layout)
            
            
            
class WorkerThread(QThread):
    UpdateStatus = pyqtSignal(str)
    UpdateProgress = pyqtSignal(str)
    
    def __init__(self, wapor_api_token, bbox, FolderLocation, CropChecked, Combo, SelectWidget, Startdate, Enddate, MasterList, vector_location, workspace):
        super().__init__()
        self.wapor_api_token = wapor_api_token 
        self.bbox = bbox 
        self.FolderLocation = FolderLocation
        self.CropChecked = CropChecked
        self.Combo = Combo
        self.SelectWidget = SelectWidget 
        self.Startdate = Startdate 
        self.Enddate = Enddate
        self.MasterList = MasterList
        self.vector_location = vector_location
        
        self.path_catalog = r'https://io.apps.fao.org/gismgr/api/v1/catalog/workspaces/'
        self.path_download = r'https://io.apps.fao.org/gismgr/api/v1/download/'
        self.path_jobs = r'https://io.apps.fao.org/gismgr/api/v1/catalog/workspaces/WAPOR/jobs/'
        self.path_query = r'https://io.apps.fao.org/gismgr/api/v1/query/'
        self.path_sign_in = r'https://io.apps.fao.org/gismgr/api/v1/iam/sign-in/'
        self.workspaces = workspace

    
    
    def Mbox(self, title, text, style):
        return ctypes.windll.user32.MessageBoxW(0, text, title, style) 
    
    requests.post(r'https://io.apps.fao.org/gismgr/api/v1/iam/sign-in/',headers = {'X-GISMGR-API-KEY':'244c28664b4d5e8babdc7b2031ee575360823721076659786f823776a48bd512681d0a6b5490fac1'})
    
    
    
    def run(self):
        x = 0
        self.UpdateStatus.emit("Status: Checking Inputs")
        #try to make new base folder
        try:
            self.base_save_folder = os.path.join(self.FolderLocation,self.workspaces + " " + datetime.datetime.now().strftime("%Y-%m-%d-%H-%M-%S"))
            os.makedirs(self.base_save_folder)
        except:
            self.Mbox('Error', 'Unable to create new folder for WaPOR download in the selected folder.', 0)
            x += 1    
        #check start date is less that end date
        if self.Enddate < self.Startdate:
            self.Mbox('Error', 'End date is earlier than the Start date.', 0)
            x += 1
        #Creats self.SelectedCubeCodes (a list of checked boxes for WaPOR request), and self.cubedict (contains info for each checked box)
        self.Selected()
        
        #Check that at least one box is checked
        if self.SelectedCubeCodes  == []:
            self.Mbox('Error', 'Must have one item checked for WaPOR request.', 0)
            x += 1

        #User inputs have been checked in the above sections so now the data requests to the WaPOR server can be started    
        if x  == 0:
            self.DownloadRequest()  
        else:
            self.UpdateStatus.emit("Status:")            
            pass

  
    def query_accessToken(self):
            resp_vp = requests.post(self.path_sign_in,headers = {'X-GISMGR-API-KEY':self.wapor_api_token})
            resp_vp = resp_vp.json()
            try:
                self.AccessToken = resp_vp['response']['accessToken']               
                self.time_expire = resp_vp['response']['expiresIn']-600 #set expire time 2 minutes earlier
                self.time_start = datetime.datetime.now().timestamp()
            except:
                
                self.Mbox( 'Error:', resp_vp['message'],0)    
    
    def CheckAccessToken(self):     
            time_now = datetime.datetime.now().timestamp()
            if time_now-self.time_start > self.time_expire:
                self.query_accessToken()       

    def LCC_Legend(self, cube_code, savefolder):
        LCCBody = pd.DataFrame.from_dict(self.cubedict[cube_code]['cubemeasure']['classes']).T
        LCCTitle = pd.DataFrame.from_dict({self.cubedict[cube_code]['cubemeasure']['code']: {'caption': self.cubedict[cube_code]['cubemeasure']['caption'], 'description': self.cubedict[cube_code]['cubemeasure']['description']} }).T
        LCCList = pd.concat([LCCTitle, LCCBody])
        LCCList.to_csv(os.path.join(savefolder,'{0} Legend.csv'.format(cube_code)) ,encoding = 'utf-8', sep = ',')
      
    def Selected(self):
        self.SelectedCubeCodes = []
        iterator = QTreeWidgetItemIterator(self.SelectWidget,  QTreeWidgetItemIterator.IteratorFlag(0x00001000) )
        while iterator.value():
            CurrentItem = iterator.value()
            if CurrentItem.childCount()  == 0:
                self.SelectedCubeCodes.append(CurrentItem.text(1))
            iterator += 1
        self.AddCubeData()
        
    def AddCubeData(self):
        self.cubedict = dict()

        for cubecode in self.SelectedCubeCodes:
                    for x in self.MasterList:
                        if x.get('code')  == cubecode:
                            self.cubedict[cubecode] = x
                            break
                    
                    #Measures
                    #provides operations pertaining to Measure resources
                    #returns a list (paged or not) of available Measure resource type items
                    #example: https://io.apps.fao.org/gismgr/api/v1/catalog/workspaces/WAPOR_2/cubes/L1_E_A/measures?overview=false&paged=false
                    request_json = (requests.get(r'{0}{1}/cubes/{2}/measures?overview=false&paged=false'.format(self.path_catalog,self.workspaces, cubecode)).json())
                    if request_json['status']  == 200:
                        self.cubedict[cubecode].update({'cubemeasure':(request_json['response'][0])})
                    else:
                        self.Mbox( 'Error' ,str(request_json['message']),0)

                    #Cube Dimensions: provides operations pertaining to CubeDimension resources
                    #get the CubeDimensions list
                    #example:https://io.apps.fao.org/gismgr/api/v1/catalog/workspaces/WAPOR_2/cubes/L1_AETI_M/dimensions?overview=false&paged=false
                    request_json = (requests.get(r'{0}{1}/cubes/{2}/dimensions?overview=false&paged=false'.format(self.path_catalog,self.workspaces,cubecode)).json())
                    if request_json['status']  == 200:
                        self.cubedict[cubecode].update({'cubedimensions':(request_json['response'])})
                    else:
                        self.Mbox( 'Error' ,str(request_json['message']),0)
        

    def DownloadRequest(self):        
            #Sets off main request code chunk
            self.UpdateStatus.emit("Status: Preparing Download")
            m = 0
            self.query_accessToken()
            for  cube_code in self.SelectedCubeCodes:
                if self.isInterruptionRequested()  == False:
                    m+= 1
                    self.UpdateProgress.emit("Progress: Starting download for Wapor data {0}.\n Item number {1} of {2}".format(cube_code, m,  str(len(self.SelectedCubeCodes))))
                    self.UpdateStatus.emit("Status: Creating Data list")
                    df_avail = self.Get_df(cube_code, self.Startdate, self.Enddate) 
                    
                    multiplier = self.cubedict[cube_code]['cubemeasure']['multiplier']
                    savefolder = os.path.join(self.base_save_folder, cube_code)
                    os.makedirs(savefolder)
                    df_avail.to_csv(os.path.join(self.base_save_folder,cube_code +' list.csv'))
                    
                   #self.ui.labelStatus.setText("Status: Constructing WaPOR Request")
                    if 'lcc' in cube_code.lower() and self.workspaces == 'WAPOR_2':
                        self.LCC_Legend(cube_code, savefolder)
                        pass
                    
                    n = 0
                    for index, row in df_avail.iterrows():
                        if self.isInterruptionRequested()  == False:
                            n+= 1            
                            self.UpdateProgress.emit("Progress: Starting download for Wapor data {0}. \nItem number {1} of {2} \nDownloading raster {3} of {4}".format(cube_code, m,  str(len(self.SelectedCubeCodes)), n,str(len(df_avail))))
                            self.UpdateStatus.emit("Status: Requesting download URL from WaPOR")
                            self.download_url = self.getCropRasterURL(cube_code, row) 
                            
                            self.UpdateStatus.emit("Status: Downloading")
                            resp = requests.get(self.download_url)
                            self.UpdateStatus.emit("Status: Correcting raster")
                            self.Tiff_Edit_Save(cube_code,  multiplier, row, savefolder, resp)
                            
            if self.isInterruptionRequested()  == False:
                self.UpdateStatus.emit("Status: Download Completed")
                self.UpdateProgress.emit("")
            else:
                self.UpdateStatus.emit("Status: Download Canceled")
                self.UpdateProgress.emit("")
                
    def Get_df(self, cube_code, Startdate, Enddate):
        time_range = '{0},{1}'.format(Startdate,Enddate)
        try:
            df_avail = self.getAvailData(cube_code,time_range)
            return df_avail
        except:
            self.Mbox( 'Error' ,'Cannot get list of available requested data',0)
            return

        
        
    
              
                
    def Tiff_Edit_Save(self, cube_code,  multiplier, row, savefolder, resp):                  
      #check this works for seasonal and non seasonal
              try:   
                  cube_dimensions = self.cubedict[cube_code]['cubedimensions']
                  rasterID_index = (len(row) - 2)
                  rasterID = row[rasterID_index]
            
                  time_code = ''
                  if any('TIME' in d.values() for d in cube_dimensions):
                      time_code_index = int(((len(row) - 2)/3))
                      time_code = row[time_code_index]
     
                  filename = '{0}{1}.tif'.format(rasterID, time_code)
                  outfilename = os.path.join(savefolder,filename)       
                  download_file = os.path.join(savefolder,'raw_{0}.tif'.format(rasterID))
                  ndays = 1
                  #By defualt dekadal data from WaPOR is an average. This allows it to give the cumulative value.
                  if any(d['code'] == 'DEKAD' for d in self.cubedict[cube_code]['cubedimensions']) and self.Combo  == 'Cumulative' and self.workspaces == 'WAPOR_2':
                      timestr = time_code
                      startdate = datetime.datetime.strptime(timestr[1:11],'%Y-%m-%d')
                      enddate = datetime.datetime.strptime(timestr[12:22],'%Y-%m-%d')
                      ndays = (enddate.timestamp()-startdate.timestamp())/86400
                  open(download_file,'wb').write(resp.content)
                  driver, NDV, xsize, ysize, GeoT, Projection = self.GetGeoInfo(download_file)
                  Array = self.OpenAsArray(download_file,nan_values = True)
                  correction = multiplier * ndays
                  
                  if(self.workspaces == 'ASIS' and cube_code != 'PHE'):
                      CorrectedArray = np.multiply(Array, correction, out = Array, where = Array < 251)
                      #attempt at creat a general solution and not a hardcoded solution.
                      # NDV_list = [NDV]
                      # for value in self.cubedict[cube_code]['additionalInfo']['flags']:
                      #     NDV_list.append(int(value['value']))
                      #     print(NDV_list)
                      # CorrectedArray = np.multiply(Array, correction, where = Array not in NDV_list)
                  else:
                      CorrectedArray = np.multiply(Array, correction, where = Array!=NDV)
                      
                  self.CreateGeoTiff(outfilename,CorrectedArray,
                                    driver, NDV, xsize, ysize, GeoT, Projection)

                  os.remove(download_file)          
                  if self.CropChecked:
                      gdal.Warp(os.path.join(savefolder,('Clip' + filename)), outfilename,cutlineDSName = self.vector_location, cropToCutline = (True), warpOptions = [ 'CUTLINE_ALL_TOUCHED=TRUE' ])#
                      os.remove(outfilename)
                      os.rename(os.path.join(savefolder,('Clip' + filename)), outfilename)
              except:
                  pass



    def getAvailData(self,cube_code,time_range):
        try:
            measure_code = self.cubedict[cube_code]['cubemeasure']['code']
            dimensions = self.cubedict[cube_code]['cubedimensions']
        except:
            self.Mbox( 'Error' ,'Cannot get cube info',0)
           
        df_dims_ls = []
        dims_ls = []
        columns_codes = ['MEASURES']
        rows_codes = []
        try:

            for item in dimensions:
                if item['type']  == 'TIME': #get time dims
                    time_dims_code = item['code']
                    df_time, avalible_data = self._query_dimensionsMembers(cube_code,time_dims_code)
                    df_dims_ls = df_dims_ls + avalible_data
                    time_dims = {
                        "code": time_dims_code,
                        "range": '[{0})'.format(time_range)
                        }
                    dims_ls.append(time_dims)
                    rows_codes.append(time_dims_code)  
                if item['type']  == 'WHAT':
                    dims_code = item['code']
                    df_dims , avalible_data = self._query_dimensionsMembers(cube_code,dims_code)
                    df_dims_ls = df_dims_ls + avalible_data
                    members_ls = df_dims['code'].tolist()
                    what_dims = {
                            "code":item['code'],
                            "values":members_ls
                            }
                    dims_ls.append(what_dims)
  
                    rows_codes.append(item['code']) 

            df = self._query_availData(cube_code,measure_code,
                             dims_ls,columns_codes,rows_codes)
        except:
            self.Mbox( 'Error' ,'Failed request cannot get list of available data',0)
            return None
        

        keys = []
        for item in rows_codes:
            keys.append(item)
            keys.append(item + '-code')
            keys.append(item + '-description')
       
   
        keys = keys + ['raster_id','bbox']
        df_dict = { i : [] for i in keys }
        for irow,row in df.iterrows():
            header_count = 0
            cell_count = 0
            for i in range(len(row)): 
                if row[i] == None:
                    break
                if row[i]['type']  == 'ROW_HEADER':
                    key_info = row[i]['value']
                    header_number = int(header_count * 3)
                    df_dict[keys[header_number]].append(key_info)
                    #df_dims_ls can get quite long
                    #potentialy could be a quicker way to iterate through.
                    for j in df_dims_ls:
                        if j['caption'] ==  key_info:
                            key= keys[header_number] + '-code'
                            df_dict[key].append(j['code'])
                            key= keys[header_number] + '-description'
                            #Not all items have a description
                            try:
                                df_dict[key].append(j['description'])
                            except:
                                df_dict[key].append('NA')
                            header_count += 1
                            break

                if row[i]['type']=='DATA_CELL':
                    if cell_count != 0:
                        k=0
                        while k < len(df_dict)-2:
                            key = keys[k]
                            df_dict[key].append(df_dict[key][-1])
                            k += 1
                        
                    raster_info=row[i]['metadata']['raster']    
                    df_dict['raster_id'].append(raster_info['id'])
                    df_dict['bbox'].append(raster_info['bbox'])
                    cell_count += 1

        df_sorted=pd.DataFrame.from_dict(df_dict)
        return df_sorted            

    

    
    def _query_dimensionsMembers(self,cube_code,dims_code):
        base_url = '{0}{1}/cubes/{2}/dimensions/{3}/members?overview=false&paged=false'       
        request_url = base_url.format(self.path_catalog,
                                    self.workspaces,
                                    cube_code,
                                    dims_code
                                    )
        resp = requests.get(request_url)
        resp_vp = resp.json()
        try:
            avail_items = resp_vp['response']
            df = pd.DataFrame.from_dict(avail_items, orient = 'columns')
        except:
            self.Mbox( 'Error' ,'Cannot get dimensions Members.'+str(resp_vp['message']),0)
        return df , avail_items


    
    def _query_availData(self,cube_code,measure_code,
                         dims_ls,columns_codes,rows_codes):                
        query_load = {
          "type": "MDAQuery_Table",              
          "params": {
            "properties": {                     
              "metadata": True,                     
              "paged": False,                 
            },
            "cube": {                            
              "workspaceCode": self.workspaces,            
              "code": cube_code,                       
              "language": "en"                      
            },
            "dimensions": dims_ls,
            "measures": [measure_code],
            "projection": {                      
              "columns": columns_codes,                               
              "rows": rows_codes
            }
          }
        }

        resp = requests.post(self.path_query, json = query_load)
        resp_vp = resp.json()
        try:
            results = resp_vp['response']['items']
            
            #Some of the responces have duplicate items in them which need to be removed 
            #the following section removes these duplicates.
            #exception writen in for where the duplicates are intentional
            if cube_code != 'EMS' and self.workspaces != 'GLEAM3':
                for item in results:
                    y = len(item)-1    
                    while y > 0:
                        if item[y] in item[:y]:
                            item.remove(item[y])
                            if y > len(item)-1:
                                y -= 1
                        else:
                            y -= 1
                            
            return pd.DataFrame(results)
        
        except:
            self.Mbox( 'Error' ,'Cannot get list of available data.'+str(resp_vp['message']),0)
        
  
            
    def getCropRasterURL(self,cube_code,
                          row):
        #Create Polygon        
        xmin,ymin,xmax,ymax = self.bbox[0], self.bbox[1], self.bbox[2], self.bbox[3]
        Polygon = [
                  [xmin,ymin],
                  [xmin,ymax],
                  [xmax,ymax],
                  [xmax,ymin],
                  [xmin,ymin]
                ]
        cube_measure_code = self.cubedict[cube_code]['cubemeasure']['code']
        dimension_params = []
        rasterID_index = (len(row) - 2)
        rasterID = row[rasterID_index]
        x=0
        while x < (len(row)-2):
            i_code = int(x+1)                         
            dimension_params.append({
            "code": row.index[x],
            "values": [row[i_code]]
            })
            x += 3
 
        #Query payload
        query_crop_raster = {
          "type": "CropRaster",
          "params": {
            "properties": {
              "outputFileName": "{0}.tif".format(rasterID),
              "cutline": True,
              "tiled": True,
              "compressed": True,
              "overviews": True
            },
            "cube": {
              "code": cube_code,
              "workspaceCode": self.workspaces,
              "language": "en"
            },
            "dimensions": dimension_params,
            "measures": [
              cube_measure_code
            ],
            "shape": {
              "type": "Polygon",
              "properties": {
                      "name": "epsg:4326" #latlon projection
                              },
              "coordinates": [
                Polygon
              ]
            }
          }
        }
        
        self.CheckAccessToken()
        resp_vp = requests.post(self.path_query,
                              headers = {'Authorization':'Bearer {0}'.format(self.AccessToken)},
                                                        json = query_crop_raster)
        resp_vp = resp_vp.json()
        try:
            job_url = resp_vp['response']['links'][0]['href']
            download_url = self._query_jobOutput(job_url)
            return download_url     
        except:
            self.Mbox( 'Error' ,'Cannot get cropped raster URL',0)
   
  
    
    def _query_jobOutput(self,job_url):
     #This method queries the WaPOR sever until the download is ready which it then returns.
     contiue = True        
     while contiue:        
         resp = requests.get(job_url)
         resp = resp.json()
         jobType = resp['response']['type'] 
                
         if resp['response']['status']  == 'COMPLETED':
             contiue = False
             if jobType  == 'CROP RASTER':
                 output = resp['response']['output']['downloadUrl']                
             elif jobType  == 'AREA STATS':
                 results = resp['response']['output']
                 output = pd.DataFrame(results['items'], columns = results['header'])
             else:
                 pass                
             return output
         if resp['response']['status']  == 'COMPLETED WITH ERRORS':
             contiue = False    
         #Method keeps pinging the sever till the request is done. Short delay to minimize the number of requests.       
         time.sleep(2)

    
    def GetGeoInfo(self, fh, subdataset = 0):

        SourceDS = gdal.Open(fh, gdal.GA_Update)
        Type = SourceDS.GetDriver().ShortName
        if Type  == 'HDF4' or Type  == 'netCDF':
            SourceDS = gdal.Open(SourceDS.GetSubDatasets()[subdataset][0])
        NDV = SourceDS.GetRasterBand(1).GetNoDataValue()
        xsize = SourceDS.RasterXSize
        ysize = SourceDS.RasterYSize
        GeoT = SourceDS.GetGeoTransform()
        Projection = osr.SpatialReference()
        Projection.ImportFromWkt(SourceDS.GetProjectionRef())
        driver = gdal.GetDriverByName(Type)
        return driver, NDV, xsize, ysize, GeoT, Projection    
    
    

    def OpenAsArray(self, fh, bandnumber = 1, dtype = 'float32', nan_values = False):

        datatypes = {"uint8": np.uint8, "int8": np.int8, "uint16": np.uint16, "int16":  np.int16, "Int16":  np.int16, "uint32": np.uint32,
        "int32": np.int32, "float32": np.float32, "float64": np.float64, "complex64": np.complex64, "complex128": np.complex128,
        "Int32": np.int32, "Float32": np.float32, "Float64": np.float64, "Complex64": np.complex64, "Complex128": np.complex128,}
        DataSet = gdal.Open(fh, gdal.GA_Update)
        Type = DataSet.GetDriver().ShortName
        if Type  == 'HDF4':
            Subdataset = gdal.Open(DataSet.GetSubDatasets()[bandnumber][0])
            NDV = int(Subdataset.GetMetadata()['_FillValue'])
        else:
            Subdataset = DataSet.GetRasterBand(bandnumber)
            NDV = Subdataset.GetNoDataValue()
        Array = Subdataset.ReadAsArray().astype(datatypes[dtype])
        if nan_values:
            Array[Array  == NDV] = np.nan
        return Array
   
    def CreateGeoTiff(self, fh, Array, driver, NDV, xsize, ysize, GeoT, Projection, explicit = True, compress = None):
        
        datatypes = {"uint8": 1, "int8": 1, "uint16": 2, "int16": 3, "Int16": 3, "uint32": 4,
        "int32": 5, "float32": 6, "float64": 7, "complex64": 10, "complex128": 11,
        "Int32": 5, "Float32": 6, "Float64": 7, "Complex64": 10, "Complex128": 11,}
        if compress != None:
            DataSet2 = driver.Create(fh,xsize,ysize,1,datatypes[Array.dtype.name], ['COMPRESS={0}'.format(compress)])
        else:
            DataSet2 = driver.Create(fh,xsize,ysize,1,datatypes[Array.dtype.name])
        if NDV is None:
            NDV = -9999
        if explicit:
            Array[np.isnan(Array)] = NDV
        DataSet2.GetRasterBand(1).SetNoDataValue(NDV)
        DataSet2.SetGeoTransform(GeoT)
        DataSet2.SetProjection(Projection.ExportToWkt())
        DataSet2.GetRasterBand(1).WriteArray(Array)
        DataSet2 = None
        if "nt" not in Array.dtype.name:
            Array[Array  == NDV] = np.nan 
            
